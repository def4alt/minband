"""YOLO instance segmentation + ByteTrack on the clip the SLAM ran on (stage 2 of tools/recon3d).

    conda run -n YOLO-CUDA-13 python tools/recon3d/detect.py <run> [--model yolo11x-seg.pt] [--imgsz 1344]

Reads <run>/clip.mp4, writes <run>/detections.json: per frame the boxes ByteTrack kept, each with
its 2D track id, COCO class, confidence, box (x1 y1 x2 y2, source pixels) and mask outline
(polygon, source pixels, simplified). Only road users are kept (person, bicycle, car, motorcycle,
bus, truck). NMS is class-agnostic: one vehicle the model scores as both car and truck is one box,
not two tracks on top of each other. The 2D track id is the association; lift.py does everything
in 3D.
"""
import argparse
import json
import pathlib

import cv2
import numpy as np
from ultralytics import YOLO

KEEP = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

ap = argparse.ArgumentParser()
ap.add_argument("run")
ap.add_argument("--model", default="yolo11x-seg.pt")
ap.add_argument("--imgsz", type=int, default=1344)
ap.add_argument("--conf", type=float, default=0.25)
args = ap.parse_args()

run = pathlib.Path(args.run)
models = pathlib.Path(__file__).parent / "models"
models.mkdir(exist_ok=True)
weights = models / args.model
model = YOLO(str(weights) if weights.exists() else args.model)
if not weights.exists() and pathlib.Path(args.model).exists():
    pathlib.Path(args.model).rename(weights)  # ultralytics downloads into the cwd; keep weights out of the repo tree

cap = cv2.VideoCapture(str(run / "clip.mp4"))
frames = []
i = 0
while True:
    ok, img = cap.read()
    if not ok:
        break
    res = model.track(img, imgsz=args.imgsz, conf=args.conf, classes=list(KEEP), persist=True, tracker="bytetrack.yaml", retina_masks=True, agnostic_nms=True, verbose=False)[0]
    dets = []
    if res.boxes is not None and res.boxes.id is not None:
        polys = res.masks.xy if res.masks is not None else [None] * len(res.boxes)
        for b, tid, cls, conf, poly in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.id.cpu().numpy(), res.boxes.cls.cpu().numpy(), res.boxes.conf.cpu().numpy(), polys):
            d = {"id": int(tid), "cls": int(cls), "conf": round(float(conf), 3), "box": [round(float(x), 1) for x in b]}
            if poly is not None and len(poly) >= 3:
                p = cv2.approxPolyDP(poly.astype(np.float32).reshape(-1, 1, 2), 1.0, True).reshape(-1, 2)
                d["poly"] = [[round(float(x), 1), round(float(y), 1)] for x, y in p]
            dets.append(d)
    frames.append({"i": i, "dets": dets})
    if i % 50 == 0:
        print(f"frame {i}: {len(dets)} tracked boxes", flush=True)
    i += 1

out = {"model": args.model, "imgsz": args.imgsz, "conf": args.conf, "classes": KEEP, "frames": frames}
(run / "detections.json").write_text(json.dumps(out))
ids = {d["id"] for f in frames for d in f["dets"]}
print(f"{len(frames)} frames, {sum(len(f['dets']) for f in frames)} boxes, {len(ids)} 2D tracks")
