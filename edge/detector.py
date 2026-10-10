"""edge/detector.py -- tiled ClikaRT object detection, one JSON line per frame on stdout.

Reuses tools/footage/detect.py's TiledDetector for tiling, cross-tile class-wise NMS,
and the COCO -> MinBand class map (KEEP / NAME_MAP). See docs/CLIKA_PLAN.md section 1.2:
clika_runtime.modelverse.DetectionModel.detect() returns already-decoded pixel boxes
(no logits/boxes tensors to post-process -- D-FINE's query decode runs inside
ClikaRT_modelverse.dll). So the backend seam here is `detect(tile, conf) -> boxes`,
not the raw `run(batch_nchw) -> ndarray` the plan assumed before that was confirmed.
ClikaTiledDetector inherits tiles()/tile_for()/nms()/classmap handling from
TiledDetector and only replaces __init__ (no ORT session) and __call__ (no manual
resize/NCHW/decode -- the model does its own preprocessing and decode).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "footage"))
import clika_runtime as crt
from clika_runtime.modelverse import LoadOptions, ModelRegistry
from detect import KEEP, NAME_MAP, TiledDetector, nms


class ClikaTiledDetector(TiledDetector):
    def __init__(self, source: str, cache_dir: str, device: str = "vulkan",
                 tile: int = 0, overlap: int = 96, conf: float = 0.25, offline: bool = True):
        opts = LoadOptions(device=crt.Device.parse(device), cache_dir=cache_dir, offline=offline)
        self.model = ModelRegistry.builtin().load_detection(source, opts)
        self.classmap = {i: NAME_MAP.get(name.lower(), -1) for i, name in enumerate(self.model.labels)}
        self.fixed, self.size = 0, 0
        self.tile, self.overlap, self.conf = tile, overlap, conf

    def __call__(self, frame: np.ndarray, raw: bool = False):
        """Nx6: x1, y1, x2, y2, conf, cls (MinBand id) in frame pixels, class-wise NMS."""
        h, w = frame.shape[:2]
        tile = self.tile_for(w, h)
        out = []
        for x0, y0, tw, th in dict.fromkeys(self.tiles(w, h)):
            crop = frame[y0:y0 + th, x0:x0 + tw]
            if tw != tile or th != tile:  # edge tile smaller than tile: pad, do not stretch
                pad = np.full((tile, tile, 3), 114, np.uint8)
                pad[:th, :tw] = crop
                crop = pad
            t = crt.Tensor.from_data(np.ascontiguousarray(crop[..., ::-1]))  # BGR -> RGB
            dets = self.model.detect(t, confidence=self.conf, max_detections=100)
            for d in dets.items:
                cls = self.classmap.get(d.label_id, -1)
                if cls in KEEP:
                    out.append((d.xmin + x0, d.ymin + y0, d.xmax + x0, d.ymax + y0, d.score, cls))
        d = np.array(out, np.float32) if out else np.zeros((0, 6), np.float32)
        kept = nms(d, self.conf)
        return (kept, d) if raw else kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--model", default="ustc-community/dfine-nano-coco")
    ap.add_argument("--cache-dir", default="tools/footage/models/clika")
    ap.add_argument("--device", default="vulkan", choices=["vulkan", "cpu"])
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--tile", type=int, default=0)
    ap.add_argument("--max-fps", type=float, default=0)
    args = ap.parse_args()

    det = ClikaTiledDetector(args.model, args.cache_dir, device=args.device,
                              tile=args.tile, conf=args.conf)
    cap = cv2.VideoCapture(args.source)
    fps_in = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, round(fps_in / args.max_fps)) if args.max_fps else 1

    n = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if n % step == 0:
            h, w = frame.shape[:2]
            rows = det(frame)
            line = {
                "t": int(time.time() * 1000),
                "frame": n,
                "w": w,
                "h": h,
                "dets": [[round(float(v), 1) for v in r[:4]] + [round(float(r[4]), 3), int(r[5])] for r in rows],
            }
            print(json.dumps(line), flush=True)
        n += 1


if __name__ == "__main__":
    main()
