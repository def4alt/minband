"""MASt3R-SLAM with a per-frame export: what the edge needs to put detections into the reconstruction.

MASt3R-SLAM's own main.py saves keyframe poses and the fused map. Lifting a detection to 3D needs
more: every frame's pose and its pixel-aligned pointmap. This runner is main.py's loop (no
visualisation, no relocalisation shortcuts) with that export added. Run it from the MASt3R-SLAM
checkout (checkpoints and config are relative to it), in its conda env:

    cd ~/MASt3R-SLAM && conda run -n mast3r-slam python <repo>/tools/recon3d/slam_export.py \
        --dataset <video.mp4 or dir of PNGs> --out <run>/slam [--subsample 1]

Writes to --out:
    frames/f_%05d.npz   X float16 [H,W,3] camera-frame pointmap (x right, y down, z forward),
                        C float16 [H,W] confidence; only frames that were tracked or keyframed
    keyframes.npz       per keyframe: frame_id, X [K,H,W,3] f16, C [K,H,W] f16, rgb [K,H,W,3] u8
    poses.json          per frame: i, t, ok, kf (keyframe it was tracked against), is_kf, and the
                        camera-to-world similarity as a 4x4 (sR | t) after the final optimisation
    meta.json           image sizes and the original -> pointmap pixel mapping

Frame poses are re-expressed against the final keyframe poses: a frame tracked against keyframe k
keeps its pose relative to k (T_k^-1 T_f at tracking time) and is re-anchored on k's optimised
pose, so the backend's later corrections reach every frame, not only the keyframes.
"""
import argparse
import json
import pathlib
import sys
import time

import lietorch
import numpy as np
import torch
import torch.multiprocessing as mp

sys.path.insert(0, str(pathlib.Path.cwd()))
from mast3r_slam.config import config, load_config, set_global_config  # noqa: E402
from mast3r_slam.dataloader import load_dataset  # noqa: E402
from mast3r_slam.frame import Mode, SharedKeyframes, SharedStates, create_frame  # noqa: E402
from mast3r_slam.mast3r_utils import load_mast3r, mast3r_inference_mono, resize_img  # noqa: E402
from mast3r_slam.tracker import FrameTracker  # noqa: E402


def copy_pose(T):
    """lietorch groups have no clone(): copy through the data tensor."""
    return lietorch.Sim3(T.data.clone())


def sim3_matrix(T):
    """lietorch Sim3 (1 element) -> 4x4 numpy with the scale folded into the rotation."""
    return T.matrix()[0].double().cpu().numpy()


def run_backend(cfg, model, states, keyframes, K):
    # main.py's backend, imported lazily so the spawn child sets its config first.
    import main as slam_main
    slam_main.run_backend(cfg, model, states, keyframes, K)


def save_pointmap(path, frame, h, w):
    X = frame.X_canon.reshape(h, w, 3).to(torch.float16).cpu().numpy()
    C = frame.get_average_conf().reshape(h, w).to(torch.float16).cpu().numpy()
    np.savez(path, X=X, C=C)


if __name__ == "__main__":
    mp.set_start_method("spawn")
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.set_grad_enabled(False)
    device = "cuda:0"

    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--config", default="config/base.yaml")
    ap.add_argument("--out", required=True)
    ap.add_argument("--subsample", type=int, default=0, help="override config dataset.subsample")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    (out / "frames").mkdir(parents=True, exist_ok=True)
    load_config(args.config)
    if args.subsample:
        config["dataset"]["subsample"] = args.subsample

    manager = mp.Manager()
    dataset = load_dataset(args.dataset)
    dataset.subsample(config["dataset"]["subsample"])
    h, w = dataset.get_img_shape()[0]
    keyframes = SharedKeyframes(manager, h, w)
    states = SharedStates(manager, h, w)

    model = load_mast3r(device=device)
    model.share_memory()
    tracker = FrameTracker(model, keyframes, device)
    backend = mp.Process(target=run_backend, args=(config, model, states, keyframes, None))
    backend.start()

    records = []      # per frame: dict, with the tracking-time poses as lietorch objects
    restarts = 0
    last_ok = lietorch.Sim3.Identity(1, device=device)
    t0 = time.time()
    i = 0
    while i < len(dataset):
        mode = states.get_mode()
        timestamp, img = dataset[i]
        if i == 0:
            src_h, src_w = img.shape[:2]
        T_WC = lietorch.Sim3.Identity(1, device=device) if i == 0 else states.get_frame().T_WC
        frame = create_frame(i, img, T_WC, img_size=dataset.img_size, device=device)
        rec = {"i": i, "t": float(timestamp), "ok": False, "kf": -1, "is_kf": False}

        if mode == Mode.INIT:
            X, C = mast3r_inference_mono(model, frame)
            frame.update_pointmap(X, C)
            keyframes.append(frame)
            states.queue_global_optimization(len(keyframes) - 1)
            states.set_mode(Mode.TRACKING)
            states.set_frame(frame)
            rec.update(ok=True, kf=0, is_kf=True, T_f=copy_pose(frame.T_WC), T_k=copy_pose(frame.T_WC))
            save_pointmap(out / "frames" / f"f_{i:05d}.npz", frame, h, w)
            records.append(rec)
            i += 1
            continue

        add_new_kf = False
        if mode == Mode.TRACKING:
            k = len(keyframes) - 1
            T_k = copy_pose(keyframes[k].T_WC)
            add_new_kf, _, try_reloc = tracker.track(frame)
            if try_reloc:
                # Tracking lost (a fast turn): no relocalisation round trip with the backend (it could
                # stall this loop); this frame starts a new keyframe from its own mono pointmap at the
                # last good pose, and the backend ties it to the previous keyframe and optimises.
                frame = create_frame(i, img, copy_pose(last_ok), img_size=dataset.img_size, device=device)
                X, C = mast3r_inference_mono(model, frame)
                frame.update_pointmap(X, C)
                keyframes.append(frame)
                states.queue_global_optimization(len(keyframes) - 1)
                tracker.reset_idx_f2k()
                restarts += 1
                print(f"frame {i}: tracking lost, new keyframe {len(keyframes) - 1} at the last good pose", flush=True)
                rec.update(ok=True, kf=len(keyframes) - 1, is_kf=True, T_f=copy_pose(frame.T_WC), T_k=copy_pose(frame.T_WC))
                save_pointmap(out / "frames" / f"f_{i:05d}.npz", frame, h, w)
            else:
                rec.update(ok=True, kf=k, T_f=copy_pose(frame.T_WC), T_k=T_k)
                save_pointmap(out / "frames" / f"f_{i:05d}.npz", frame, h, w)
                last_ok = copy_pose(frame.T_WC)
            states.set_frame(frame)
        elif mode == Mode.RELOC:
            X, C = mast3r_inference_mono(model, frame)
            frame.update_pointmap(X, C)
            states.set_frame(frame)
            states.queue_reloc()
            while True:  # one relocalisation attempt per frame, like single-thread mode
                with states.lock:
                    if states.reloc_sem.value == 0:
                        break
                time.sleep(0.01)
            if states.get_mode() == Mode.TRACKING:
                # Relocalised: the frame was appended as a keyframe with a pose.
                k = len(keyframes) - 1
                rec.update(ok=True, kf=k, is_kf=True, T_f=copy_pose(keyframes[k].T_WC), T_k=copy_pose(keyframes[k].T_WC))
                save_pointmap(out / "frames" / f"f_{i:05d}.npz", frame, h, w)

        if add_new_kf:
            keyframes.append(frame)
            states.queue_global_optimization(len(keyframes) - 1)
            rec.update(is_kf=True, kf=len(keyframes) - 1, T_k=copy_pose(frame.T_WC))
        records.append(rec)
        if i % 30 == 0:
            print(f"frame {i}/{len(dataset)}  kfs {len(keyframes)}  {i / (time.time() - t0):.1f} fps", flush=True)
        i += 1

    # Let the backend finish optimising every queued keyframe before reading the final poses.
    while True:
        with states.lock:
            if len(states.global_optimizer_tasks) == 0:
                break
        time.sleep(0.05)
    time.sleep(0.5)
    states.set_mode(Mode.TERMINATED)
    backend.join()

    n_kf = len(keyframes)
    final = [copy_pose(keyframes[k].T_WC) for k in range(n_kf)]
    poses = []
    for r in records:
        p = {k: r[k] for k in ("i", "t", "ok", "kf", "is_kf")}
        if r["ok"]:
            k = r["kf"]
            T = final[k] * r["T_k"].inv() * r["T_f"] if not r["is_kf"] else final[k]
            p["T"] = sim3_matrix(T).tolist()
        poses.append(p)
    (out / "poses.json").write_text(json.dumps(poses))

    kf = {"frame_id": [], "X": [], "C": [], "rgb": []}
    for k in range(n_kf):
        f = keyframes[k]
        kf["frame_id"].append(int(f.frame_id))
        kf["X"].append(f.X_canon.reshape(h, w, 3).to(torch.float16).cpu().numpy())
        kf["C"].append(f.get_average_conf().reshape(h, w).to(torch.float16).cpu().numpy())
        kf["rgb"].append((f.uimg.reshape(h, w, 3) * 255).clamp(0, 255).to(torch.uint8).cpu().numpy())
    np.savez(out / "keyframes.npz", frame_id=np.array(kf["frame_id"]), X=np.stack(kf["X"]), C=np.stack(kf["C"]), rgb=np.stack(kf["rgb"]))

    _, (sw, sh, cw, ch) = resize_img(np.zeros((src_h, src_w, 3)), dataset.img_size, return_transformation=True)
    meta = {"dataset": args.dataset, "src_w": int(src_w), "src_h": int(src_h), "pm_w": int(w), "pm_h": int(h),
            # original pixel (u, v) -> pointmap pixel ((u / sw) - cw, (v / sh) - ch)
            "scale_w": float(sw), "scale_h": float(sh), "crop_w": float(cw), "crop_h": float(ch),
            "subsample": int(config["dataset"]["subsample"]), "frames": len(records), "keyframes": n_kf,
            "tracked": sum(r["ok"] for r in records), "tracking_restarts": restarts, "seconds": round(time.time() - t0, 1)}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta), flush=True)
