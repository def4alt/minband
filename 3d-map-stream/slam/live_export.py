"""Live snapshot exporter for the 10 kbit/s map-streaming demo.

After every keyframe insert main.py (flag --live-export DIR) calls
export_snapshot(); it writes DIR/snap_%04d.npz with

    pts  float32 [N,3]  world (SLAM) frame, conf > c_conf_threshold,
                        at most KF_MAX_POINTS per keyframe
    rgb  uint8   [N,3]
    traj float64 [K,8]  rows (t x y z qx qy qz qw) for keyframes 0..K-1
    kf   int            = K-1

The file is written to snap_%04d.tmp.npz and os.replace()d into place so a
reader never sees a partial file.
"""
import os
import pathlib
import struct
import time

import lietorch
import numpy as np
import torch

from mast3r_slam.config import config
from mast3r_slam.geometry import constrain_points_to_ray
from mast3r_slam.lietorch_utils import as_SE3

KF_MAX_POINTS = int(os.environ.get("LIVE_KF_MAX_POINTS", 150_000))  # per-keyframe cap (a global cap would thin later keyframes)


@torch.no_grad()
def export_snapshot(keyframes, timestamps, c_conf_threshold, outdir, idx):
    """Write one snapshot; returns (path, n_points, seconds)."""
    t0 = time.time()
    outdir = pathlib.Path(outdir)
    outdir.mkdir(exist_ok=True, parents=True)

    pts, rgb, traj = [], [], []; kf_off = [0]
    K = len(keyframes)
    for i in range(K):
        # The keyframe's X / C / T_WC are views into shared memory that the backend overwrites during global
        # optimisation: read everything of one keyframe under the shared lock and copy it, so pose, points and
        # confidences of the snapshot are mutually consistent (the sender never re-sends a keyframe).
        with keyframes.lock:
            kf = keyframes[i]
            X_canon = kf.X_canon.clone()
            T_WC = lietorch.Sim3(kf.T_WC.data.clone())
            conf = kf.get_average_conf().clone()
            uimg = kf.uimg.clone()
            frame_id = kf.frame_id
            K_cal = kf.K if config["use_calib"] else None
            img_shape = kf.img_shape.clone()
        if config["use_calib"]:
            X_canon = constrain_points_to_ray(
                img_shape.flatten()[:2], X_canon[None], K_cal
            ).squeeze(0)
        pW = T_WC.act(X_canon).reshape(-1, 3)
        valid = conf.reshape(-1) > c_conf_threshold
        p_kf = pW[valid].float().cpu().numpy()
        col = (uimg.reshape(-1, 3) * 255).clamp(0, 255).to(torch.uint8)
        c_kf = col[valid.cpu()].numpy()
        if len(p_kf) > KF_MAX_POINTS:  # cap per keyframe (deterministic per index): later keyframes keep full density
            sel = np.sort(np.random.default_rng(i).choice(len(p_kf), KF_MAX_POINTS, replace=False))
            p_kf, c_kf = p_kf[sel], c_kf[sel]
        pts.append(p_kf); rgb.append(c_kf); kf_off.append(kf_off[-1] + len(p_kf))
        t = float(timestamps[frame_id])
        x, y, z, qx, qy, qz, qw = as_SE3(T_WC).data.cpu().numpy().reshape(-1)
        traj.append([t, x, y, z, qx, qy, qz, qw])

    pts = np.concatenate(pts, 0).astype(np.float32) if pts else np.zeros((0, 3), np.float32)
    rgb = np.concatenate(rgb, 0).astype(np.uint8) if rgb else np.zeros((0, 3), np.uint8)
    traj = np.asarray(traj, dtype=np.float64).reshape(-1, 8)

    final = outdir / f"snap_{idx:04d}.npz"
    tmp = outdir / f"snap_{idx:04d}.tmp.npz"
    with open(tmp, "wb") as f:
        np.savez(f, pts=pts, rgb=rgb, traj=traj, kf=np.int64(K - 1), kf_off=np.asarray(kf_off, np.int64))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return final, len(pts), time.time() - t0


POSE_FILE = "pose_live.bin"  # <Q n><8d t x y z qx qy qz qw>: newest TRACKED frame pose (world/SLAM frame, same as traj)


@torch.no_grad()
def export_pose(T_WC, outdir, n):
    """Write the current frame's pose (every tracked frame, throttled by the caller); atomic replace, ~0.1 ms."""
    x, y, z, qx, qy, qz, qw = as_SE3(T_WC).data.cpu().numpy().reshape(-1)[:7]
    outdir = pathlib.Path(outdir); tmp = outdir / (POSE_FILE + ".tmp")
    with open(tmp, "wb") as f:
        f.write(struct.pack("<Q8d", int(n), time.time(), x, y, z, qx, qy, qz, qw))
    os.replace(tmp, outdir / POSE_FILE)
