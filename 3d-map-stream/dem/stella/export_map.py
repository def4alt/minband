"""stella_vslam msgpack map -> the files ../orbslam3's ExportMonoMap writes, so fuse.py runs on either SLAM.

usage: python export_map.py OUT_DIR    (reads OUT_DIR/map.msg; needs msgpack)
writes KeyFrameTrajectory.txt (TUM, Twc), mappoints.txt (x y z per row), kf_observations.txt (kf_id t row u v).
"""
import os, sys, msgpack, numpy as np
from scipy.spatial.transform import Rotation

out = sys.argv[1]
m = msgpack.unpackb(open(os.path.join(out, "map.msg"), "rb").read(), strict_map_key=False)
kfs = sorted(m["keyframes"].items(), key=lambda kv: kv[1]["ts"])
lms = m["landmarks"]
row_of = {int(i): r for r, i in enumerate(sorted(int(k) for k in lms))}
np.savetxt(os.path.join(out, "mappoints.txt"), np.array([lms[k]["pos_w"] for k in sorted(lms, key=int)]), fmt="%.6f")

with open(os.path.join(out, "KeyFrameTrajectory.txt"), "w") as tr, open(os.path.join(out, "kf_observations.txt"), "w") as ob:
    ob.write("# kf_id timestamp mappoint_row u v\n")
    for kid, k in kfs:
        R_cw = Rotation.from_quat(k["rot_cw"]).as_matrix(); t_cw = np.array(k["trans_cw"])
        c = -R_cw.T @ t_cw; q = Rotation.from_matrix(R_cw.T).as_quat()  # Twc, quaternion x y z w
        tr.write(f"{k['ts']:.6f} " + " ".join(f"{v:.9f}" for v in [*c, *q]) + "\n")
        for kp, lm in zip(k["undist_keypts"], k["lm_ids"]):
            if lm >= 0 and lm in row_of:
                ob.write(f"{kid} {k['ts']:.6f} {row_of[lm]} {kp['pt'][0]:.2f} {kp['pt'][1]:.2f}\n")
print(f"{out}: {len(kfs)} keyframes, {len(lms)} landmarks")
