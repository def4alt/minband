"""Run the phone's own detector on a video and write its boxes, so the Android tracking chain
(Lift3D -> Tracker -> OverlayMotion) can be replayed on the JVM at any detector rate.

Mirrors `android/.../Detector.kt` / `TfliteDetector.kt` exactly: letterbox into the 256x256 input
(grey 0x72 padding, no stretch), RGB 0..1, the `yolov8n-256.onnx` asset without NMS, confidence
0.35 (people 0.25), per-class NMS at IoU 0.45, at most 50 boxes, only the 9 tracked COCO classes. Boxes are
written as x, y, w, h normalized to the (upright) frame, which is what the phone feeds Lift3D.

    tools/footage/.venv/Scripts/python tools/movers/phone_detect.py tools/footage/clips/vtest.avi \
        --model android/app/src/main/assets/yolov8n-256.onnx \
        --out android/app/src/test/resources/replay/vtest-yolov8n-256.csv

The CSV's first line is a comment with the frame size and rate; `PhoneReplayTest.kt` reads it.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

TRACKED = {0, 24, 26, 39, 41, 56, 62, 63, 67}   # person backpack handbag bottle cup chair tv laptop cell phone
CONF = 0.35
PERSON_CONF = 0.25                               # TrackedClass.confidence: people lower, see Models.kt
IOU = 0.45
MAX_BOXES = 50
PAD = 0x72


def letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float, float, float, float]:
    h, w = frame.shape[:2]
    scale = min(size / w, size / h)
    lw, lh = w * scale, h * scale                     # the phone keeps these as floats
    dx, dy = (size - lw) / 2.0, (size - lh) / 2.0
    resized = cv2.resize(frame, (int(round(lw)), int(round(lh))), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), PAD, np.uint8)
    x0, y0 = int(round(dx)), int(round(dy))
    canvas[y0:y0 + resized.shape[0], x0:x0 + resized.shape[1]] = resized
    return canvas, lw, lh, dx, dy


def iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def decode(out: np.ndarray, size: int, lw: float, lh: float, dx: float, dy: float) -> list[tuple[int, float, float, float, float, float]]:
    """`out`: [4 + 80, N] cx cy w h in input pixels then class scores -> (cls, conf, x, y, w, h)."""
    scores = out[4:]
    best = scores.argmax(axis=0)
    conf = scores[best, np.arange(out.shape[1])]
    cands = []
    for j in np.nonzero(conf >= PERSON_CONF)[0]:
        c = int(best[j])
        if c not in TRACKED or conf[j] < (PERSON_CONF if c == 0 else CONF):
            continue
        cx, cy, bw, bh = out[0, j], out[1, j], out[2, j], out[3, j]
        x0 = min(max((cx - bw / 2 - dx) / lw, 0.0), 1.0)
        y0 = min(max((cy - bh / 2 - dy) / lh, 0.0), 1.0)
        x1 = min(max((cx + bw / 2 - dx) / lw, 0.0), 1.0)
        y1 = min(max((cy + bh / 2 - dy) / lh, 0.0), 1.0)
        if x1 - x0 < 1e-3 or y1 - y0 < 1e-3:
            continue
        cands.append((c, float(conf[j]), float(x0), float(y0), float(x1 - x0), float(y1 - y0)))
    cands.sort(key=lambda r: -r[1])
    kept: list[tuple[int, float, float, float, float, float]] = []
    for r in cands:
        if len(kept) >= MAX_BOXES:
            break
        if any(k[0] == r[0] and iou(k[2:], r[2:]) > IOU for k in kept):
            continue
        kept.append(r)
    return kept


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("video")
    ap.add_argument("--model", required=True, help="the phone's ONNX asset (yolov8n-256.onnx)")
    ap.add_argument("--out", required=True, help="CSV to write")
    ap.add_argument("--crop", type=float, default=1.0,
                    help="keep this central fraction of the frame before the letterbox (1 = whole frame)")
    args = ap.parse_args()

    sess = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    size = int(inp.shape[2])
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"cannot open {args.video}", file=sys.stderr)
        return 1
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cw, ch = int(round(w * args.crop)), int(round(h * args.crop))
    cx0, cy0 = (w - cw) // 2, (h - ch) // 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = 0
    n_boxes = 0
    with out_path.open("w", newline="") as f:
        f.write(f"# video={Path(args.video).name} width={cw} height={ch} fps={fps:.3f} model={Path(args.model).name} input={size} crop={args.crop}\n")
        wr = csv.writer(f)
        wr.writerow(["frame", "t", "cls", "conf", "x", "y", "w", "h"])
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if args.crop < 1.0:
                frame = frame[cy0:cy0 + ch, cx0:cx0 + cw]
            canvas, lw, lh, dx, dy = letterbox(frame, size)
            rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            x = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None])          # NCHW
            out = sess.run(None, {inp.name: x})[0][0]
            if out.shape[0] > out.shape[1]:                                   # [N, 84] export
                out = out.T
            t = n_frames / fps
            for (c, conf, bx, by, bw, bh) in decode(out, size, lw, lh, dx, dy):
                wr.writerow([n_frames, f"{t:.3f}", c, f"{conf:.3f}", f"{bx:.4f}", f"{by:.4f}", f"{bw:.4f}", f"{bh:.4f}"])
                n_boxes += 1
            n_frames += 1
            if n_frames % 100 == 0:
                print(f"frame {n_frames} ({t:.1f} s): {n_boxes} boxes so far", file=sys.stderr)
    print(f"{n_frames} frames at {fps:.1f} fps, {n_boxes} boxes ({n_boxes / max(n_frames, 1):.1f} per frame) -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
