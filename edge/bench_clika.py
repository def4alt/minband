"""CLIKA_SC1 benchmark: ClikaTiledDetector + DepthModel fps on real MEVA 4K frames.

No network, no model download (both already cached by the phase's fetch step). Reads frames
once from the MEVA clip, times `ClikaTiledDetector.__call__` / `DepthModel.predict` over a
fixed frame budget per config so a slow CPU config can't run away.

    .venv/Scripts/python.exe edge/bench_clika.py tools/footage/clips/<clip>.mp4
"""
import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "footage"))
import clika_runtime as crt
from clika_runtime.modelverse import LoadOptions, ModelRegistry
from detector import ClikaTiledDetector, check_device

CACHE = "tools/footage/models/clika"
DET_MODEL = "ustc-community/dfine-nano-coco"
DEPTH_MODEL = "depth-anything/Depth-Anything-V2-Small-hf"


def read_frames(path: str, n: int, start: int = 300):
    """n decoded frames starting `start` frames in (skips black lead-in / initial focus hunting)."""
    cap = cv2.VideoCapture(path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    frames = []
    for _ in range(n):
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    return frames


def bench_detector(frames, device, tile, label, warmup=1):
    det = ClikaTiledDetector(DET_MODEL, CACHE, device=device, tile=tile, allow_cpu_fallback=True)
    h, w = frames[0].shape[:2]
    n_tiles = len(list(dict.fromkeys(det.tiles(w, h))))
    for f in frames[:warmup]:
        det(f)
    timed = frames[warmup:]
    t0 = time.perf_counter()
    for f in timed:
        det(f)
    dt = time.perf_counter() - t0
    fps = len(timed) / dt if dt else float("nan")
    print(f"[detector] {label:<28} device={str(det.model.device):<10} {w}x{h} tile={det.tile_for(w, h)}px "
          f"n_tiles={n_tiles:<3} frames={len(timed):<3} total={dt:6.2f}s fps={fps:6.3f} "
          f"ms/tile={1000 * dt / (len(timed) * n_tiles):6.2f}")
    return fps, n_tiles


def bench_depth(frames, device, label, warmup=1):
    opts = LoadOptions(device=crt.Device.parse(device), cache_dir=CACHE, offline=True)
    model = ModelRegistry.builtin().load_depth(DEPTH_MODEL, opts)
    check_device(device, model.device, allow_cpu_fallback=True)
    for f in frames[:warmup]:
        model.predict(crt.Tensor.from_data(np.ascontiguousarray(f[..., ::-1])))
    timed = frames[warmup:]
    t0 = time.perf_counter()
    for f in timed:
        model.predict(crt.Tensor.from_data(np.ascontiguousarray(f[..., ::-1])))
    dt = time.perf_counter() - t0
    fps = len(timed) / dt if dt else float("nan")
    print(f"[depth]    {label:<28} device={str(model.device):<10} frames={len(timed):<3} "
          f"total={dt:6.2f}s fps={fps:6.3f}")
    return fps, model


def bench_interleaved(frames_det, frames_depth, device):
    """Detector + depth model sharing the same iGPU, calls interleaved frame-by-frame, to see
    whether the depth model starves the detector (or vice versa) versus running each alone."""
    det = ClikaTiledDetector(DET_MODEL, CACHE, device=device, tile=0, allow_cpu_fallback=True)
    opts = LoadOptions(device=crt.Device.parse(device), cache_dir=CACHE, offline=True)
    depth = ModelRegistry.builtin().load_depth(DEPTH_MODEL, opts)
    det(frames_det[0])
    depth.predict(crt.Tensor.from_data(np.ascontiguousarray(frames_depth[0][..., ::-1])))
    n = min(len(frames_det), len(frames_depth)) - 1
    det_t = depth_t = 0.0
    for i in range(1, n + 1):
        t0 = time.perf_counter(); det(frames_det[i]); det_t += time.perf_counter() - t0
        t0 = time.perf_counter()
        depth.predict(crt.Tensor.from_data(np.ascontiguousarray(frames_depth[i][..., ::-1])))
        depth_t += time.perf_counter() - t0
    print(f"[combined] detector+depth interleaved, {n} frames each: "
          f"detector fps={n / det_t:6.3f} depth fps={n / depth_t:6.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip")
    ap.add_argument("--det-frames", type=int, default=20)
    ap.add_argument("--cpu-frames", type=int, default=3, help="CPU detector is ~tens of seconds/tile at 4K: keep small")
    ap.add_argument("--depth-frames", type=int, default=20)
    args = ap.parse_args()

    native = read_frames(args.clip, args.det_frames + 1)
    downscaled = [cv2.resize(f, (1920, 1080), interpolation=cv2.INTER_AREA) for f in native]
    cpu_frames = native[:args.cpu_frames + 1]

    print(f"\n=== detection: native 4K, auto tile (tile=0) ===")
    bench_detector(native, "vulkan", 0, "vulkan native 4K")

    print(f"\n=== detection: native 4K, larger tile (tile=1280) ===")
    bench_detector(native, "vulkan", 1280, "vulkan native 4K tile=1280")

    print(f"\n=== detection: downscaled to 1920x1080, auto tile ===")
    bench_detector(downscaled, "vulkan", 0, "vulkan 1080p downscaled")

    print(f"\n=== detection: native 4K on CPU ({args.cpu_frames} frames only) ===")
    bench_detector(cpu_frames, "cpu", 0, "cpu native 4K")

    print(f"\n=== depth: native 4K, vulkan ===")
    depth_frames = native[:args.depth_frames + 1]
    bench_depth(depth_frames, "vulkan", "vulkan native 4K")

    print(f"\n=== combined: detector + depth on vulkan, interleaved ===")
    bench_interleaved(native, depth_frames, "vulkan")


if __name__ == "__main__":
    main()
