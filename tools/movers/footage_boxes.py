"""Turn a `tools/footage` run (drone footage: `track.py detect` then `track.py track`) into a replay
CSV for the Android tracking chain (`PhoneReplayTest`), the same shape `phone_detect.py` writes:
one row per box, `frame,t,cls,conf,x,y,w,h` normalized to the reference frame (then the box
in its own frame, `ox,oy,ow,oh`, for render_replay.py), and a header
line with the frame size, rate and the ground fit's camera (focal length in pixels, pitch, height),
so the JVM test can lift the boxes to the ground the way the phone does and judge who moves.

    .venv/bin/python track.py detect clips/<clip>.mp4 --model yolov8n.onnx --start 0 --end 60 --out ../../runs/footage/<run>
    .venv/bin/python track.py track ../../runs/footage/<run>
    python tools/movers/footage_boxes.py runs/footage/<run> --out android/app/src/test/resources/replay/drone-<name>.csv

Each box is mapped through its frame's homography onto the reference frame (the drone drifts; the
registration undoes that), so the replay sees a fixed camera. People by default; `--classes
0,2,3,5,7` adds cars, motorcycles, buses and trucks (the chain tracks them; the phone's own
detector does not report them).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run", help="tools/footage run directory (detections.npy, detect.json, summary.json)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--classes", default="0", help="comma-separated COCO class ids to keep (default: person; 0,2,3,5,7 adds vehicles)")
    ap.add_argument("--max-height", type=float, default=0.0,
                    help="drop person boxes taller than this fraction of the frame: the tile detector's junk (on the MEVA clip people are under 0.03 of a 4K frame at 28 m up; with yolov8n, 0.05 removed 1564 boxes of 108-172 px that were a third of the movers)")
    ap.add_argument("--person-on-vehicle", type=float, default=0.0,
                    help="drop a person box this much (fraction of its area) inside a vehicle box of the same frame: a small detector calls the edge of a parked car a person, and that box hops between neighbouring cars (yolov8n: 40 of 103 movers had sat on a car)")
    ap.add_argument("--max-vehicle-width", type=float, default=0.0,
                    help="drop vehicle boxes wider than this fraction of the frame (MEVA, 28 m up: cars are 0.02-0.05 of a 4K frame; YOLO26s put a 0.13-wide 'car' on an empty road)")
    ap.add_argument("--vehicle-min-conf", type=float, default=0.0,
                    help="drop vehicle boxes below this confidence (YOLO26s on MEVA: cars score a median 0.6; the boxes on roof edges and between two parked cars 0.2-0.3)")
    ap.add_argument("--max-vehicle-height", type=float, default=0.0,
                    help="drop vehicle boxes taller than this fraction of the frame (a 'truck' over a roof edge was 0.2 of it; cars from 28 m up are under 0.06)")
    ap.add_argument("--bus-min-conf", type=float, default=0.0,
                    help="drop bus boxes below this confidence (the clip has no bus; its 'buses' were two parked cars or a building's edge)")
    ap.add_argument("--no-mti", action="store_true", help="ignore detections-mti.npy (track.py mti) even when the run has it")
    ap.add_argument("--ground-from", help="take the camera's ground fit from this run's summary.json instead (same clip, another detector: the fit comes from people's box widths, which differ between models)")
    args = ap.parse_args()

    dets = np.load(os.path.join(args.run, "detections.npy"))
    meta = json.load(open(os.path.join(args.run, "detect.json")))
    summary = json.load(open(os.path.join(args.run, "summary.json")))
    ground = json.load(open(os.path.join(args.ground_from, "summary.json")))["ground"] if args.ground_from else summary["ground"]
    homs = {int(k): np.array(v, np.float64) for k, v in meta["homographies"].items()}
    keep = {int(c) for c in args.classes.split(",")}
    w, h, fps = meta["width"], meta["height"], meta["fps"]
    f0 = meta.get("start_frame", 0)

    # One box per vehicle: the detector's NMS is per class, so a car it also calls a truck keeps
    # both boxes (two tracks on one car that trade its hits). Across the vehicle classes, keep the
    # more confident of two boxes overlapping by IoU >= 0.5.
    def iou(a, b):
        ix = max(0.0, min(a[3], b[3]) - max(a[1], b[1])); iy = max(0.0, min(a[4], b[4]) - max(a[2], b[2]))
        inter = ix * iy
        return inter / max(1e-6, (a[3] - a[1]) * (a[4] - a[2]) + (b[3] - b[1]) * (b[4] - b[2]) - inter)
    by_frame: dict[int, list] = {}
    for k, row in enumerate(dets):
        if int(row[6]) in (2, 3, 5, 7):
            by_frame.setdefault(int(row[0]), []).append((k, row))
    drop_ids = set()
    vehicles: dict[int, list] = {}
    for f, rows in by_frame.items():
        kept = []
        for k, row in sorted(rows, key=lambda r: -r[1][5]):
            if any(iou(row, kr) >= 0.5 for kr in kept):
                drop_ids.add(k); continue
            kept.append(row)
        vehicles[f] = [kr[1:5] for kr in kept]

    def inside_vehicle(frame, x1, y1, x2, y2):
        area = max(1e-6, (x2 - x1) * (y2 - y1))
        for vx1, vy1, vx2, vy2 in vehicles.get(frame, ()):
            ix = max(0.0, min(x2, vx2) - max(x1, vx1)); iy = max(0.0, min(y2, vy2) - max(y1, vy1))
            if ix * iy / area >= args.person_on_vehicle:
                return True
        return False

    # Pixel motion (track.py mti: registered frame differencing 0.3 s apart), per frame. A box's
    # `mti` is the largest share of a moving blob's area that lies inside it (0 still, -1 unknown).
    mti_path = os.path.join(args.run, "detections-mti.npy")
    blobs: dict[int, list] = {}
    if os.path.exists(mti_path) and not args.no_mti:
        for row in np.load(mti_path):
            blobs.setdefault(int(row[0]), []).append(row[1:5])

    def motion_in(frame, x1, y1, x2, y2):
        if not blobs: return -1.0
        best = 0.0
        for bx1, by1, bx2, by2 in blobs.get(frame, ()):
            ix = max(0.0, min(x2, bx2) - max(x1, bx1)); iy = max(0.0, min(y2, by2) - max(y1, by1))
            # Intersection over the smaller of the two: a moving car's blob is the union of its
            # positions 0.3 s apart (longer than the box), a walker's blob may be just the legs.
            best = max(best, ix * iy / max(1e-6, min((bx2 - bx1) * (by2 - by1), (x2 - x1) * (y2 - y1))))
        return best

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    n = dropped = dropped_vehicles = 0
    out_rows = []
    for k, row in enumerate(dets):
        frame, x1, y1, x2, y2, conf, cls = row
        if int(cls) not in keep:
            continue
        if int(cls) in (2, 3, 5, 7) and (
                (args.max_vehicle_width and (x2 - x1) / w > args.max_vehicle_width)
                or (args.max_vehicle_height and (y2 - y1) / h > args.max_vehicle_height)
                or conf < args.vehicle_min_conf
                or (int(cls) == 5 and conf < args.bus_min_conf)):
            dropped_vehicles += 1; continue
        if k in drop_ids:
            dropped_vehicles += 1; continue
        if int(cls) == 0 and args.max_height and (y2 - y1) / h > args.max_height:
            dropped += 1; continue
        if int(cls) == 0 and args.person_on_vehicle and inside_vehicle(int(frame), x1, y1, x2, y2):
            dropped += 1; continue
        H = homs.get(int(frame))
        if H is None:
            continue
        # The four corners through the homography, then their bounding box (reference pixels).
        pts = np.array([[x1, y1, 1], [x2, y1, 1], [x1, y2, 1], [x2, y2, 1]], np.float64).T
        q = H @ pts
        if (q[2] <= 1e-9).any():
            continue
        q = q[:2] / q[2]
        # Not clipped to the reference frame: when the drone turns, part of the view lies outside
        # it, and clipping dropped everything there (a driving car on the left of the view, and
        # later every car on that side). Absurd mappings (near the horizon) are dropped.
        if q[0].min() < -2 * w or q[0].max() > 3 * w or q[1].min() < -2 * h or q[1].max() > 3 * h:
            continue
        out_rows.append((int(frame) - f0, conf, int(cls), q[0].min(), q[1].min(), q[0].max(), q[1].max(),
                         x1, y1, x2, y2, motion_in(int(frame), x1, y1, x2, y2)))

    # The replay's virtual image: the reference frame grown to hold every box, with the camera's
    # principal point moved to match (the JVM replay reads cam_cx / cam_cy).
    cx0 = min([0.0] + [r[3] for r in out_rows]); cy0 = min([0.0] + [r[4] for r in out_rows])
    cx1 = max([float(w)] + [r[5] for r in out_rows]); cy1 = max([float(h)] + [r[6] for r in out_rows])
    W, Hc = cx1 - cx0, cy1 - cy0
    with open(args.out, "w", newline="") as fh:
        fh.write(f"# video={meta['video']} width={int(round(W))} height={int(round(Hc))} fps={fps:.3f} model={meta.get('model', '?')} "
                 f"cam_f={ground['f_px']:.1f} cam_pitch={ground['pitch_deg']:.2f} cam_height={ground['height_m']:.2f} "
                 f"cam_cx={w / 2 - cx0:.1f} cam_cy={h / 2 - cy0:.1f} source=footage\n")
        wr = csv.writer(fh)
        # ox..oh: the same box in its own video frame (normalized), for drawing the replay's
        # verdicts back onto the footage (`render_replay.py`); the JVM reader ignores them.
        wr.writerow(["frame", "t", "cls", "conf", "x", "y", "w", "h", "ox", "oy", "ow", "oh", "mti"])
        for fr, conf, cls, qx0, qy0, qx1, qy1, x1, y1, x2, y2, mot in out_rows:
            wr.writerow([fr, f"{fr / fps:.3f}", cls, f"{conf:.3f}", f"{(qx0 - cx0) / W:.5f}", f"{(qy0 - cy0) / Hc:.5f}",
                         f"{(qx1 - qx0) / W:.5f}", f"{(qy1 - qy0) / Hc:.5f}",
                         f"{x1 / w:.4f}", f"{y1 / h:.4f}", f"{(x2 - x1) / w:.4f}", f"{(y2 - y1) / h:.4f}", f"{mot:.2f}"])
            n += 1
    print(f"{n} boxes ({dropped} person boxes and {dropped_vehicles} duplicate or implausible vehicle boxes dropped) -> {args.out} (camera f {ground['f_px']:.0f} px, pitch {ground['pitch_deg']:.1f} deg, {ground['height_m']:.1f} m up)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
