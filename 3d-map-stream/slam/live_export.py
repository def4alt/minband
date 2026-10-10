"""Live snapshot exporter for the 10 kbit/s map-streaming demo.

After every keyframe insert main.py (flag --live-export DIR) calls
export_snapshot(); it writes DIR/snap_%04d.npz with

    pts  float32 [N,3]  world (SLAM) frame, conf > c_conf_threshold,
                        randomly subsampled to <= MAX_POINTS
    rgb  uint8   [N,3]
    traj float64 [K,8]  rows (t x y z qx qy qz qw) for keyframes 0..K-1
    kf   int            = K-1

The file is written to snap_%04d.tmp.npz and os.replace()d into place so a
reader never sees a partial file.
"""
import os
import pathlib
import time

import numpy as np
import torch

from mast3r_slam.config import config
from mast3r_slam.geometry import constrain_points_to_ray
from mast3r_slam.lietorch_utils import as_SE3

MAX_POINTS = 600_000
_rng = np.random.default_rng(0)


@torch.no_grad()
def export_snapshot(keyframes, timestamps, c_conf_threshold, outdir, idx):
    """Write one snapshot; returns (path, n_points, seconds)."""
    t0 = time.time()
    outdir = pathlib.Path(outdir)
    outdir.mkdir(exist_ok=True, parents=True)

    pts, rgb, traj = [], [], []; kf_off = [0]
    K = len(keyframes)
    for i in range(K):
        kf = keyframes[i]
        X_canon = kf.X_canon
        if config["use_calib"]:
            X_canon = constrain_points_to_ray(
                kf.img_shape.flatten()[:2], X_canon[None], kf.K
            ).squeeze(0)
        pW = kf.T_WC.act(X_canon).reshape(-1, 3)
        valid = kf.get_average_conf().reshape(-1) > c_conf_threshold
        pts.append(pW[valid].float().cpu().numpy())
        col = (kf.uimg.reshape(-1, 3) * 255).clamp(0, 255).to(torch.uint8)
        rgb.append(col[valid.cpu()].numpy()); kf_off.append(kf_off[-1] + len(pts[-1]))
        t = float(timestamps[kf.frame_id])
        x, y, z, qx, qy, qz, qw = as_SE3(kf.T_WC).data.cpu().numpy().reshape(-1)
        traj.append([t, x, y, z, qx, qy, qz, qw])

    pts = np.concatenate(pts, 0).astype(np.float32) if pts else np.zeros((0, 3), np.float32)
    rgb = np.concatenate(rgb, 0).astype(np.uint8) if rgb else np.zeros((0, 3), np.uint8)
    if len(pts) > MAX_POINTS:  # order-preserving subsample; keyframe offsets are remapped onto the kept points
        sel = np.sort(_rng.choice(len(pts), MAX_POINTS, replace=False))
        pts, rgb = pts[sel], rgb[sel]
        kf_off = np.searchsorted(sel, np.asarray(kf_off)).tolist()
    traj = np.asarray(traj, dtype=np.float64).reshape(-1, 8)

    final = outdir / f"snap_{idx:04d}.npz"
    tmp = outdir / f"snap_{idx:04d}.tmp.npz"
    with open(tmp, "wb") as f:
        np.savez(f, pts=pts, rgb=rgb, traj=traj, kf=np.int64(K - 1), kf_off=np.asarray(kf_off, np.int64))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return final, len(pts), time.time() - t0
