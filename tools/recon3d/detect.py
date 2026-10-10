"""YOLO detection (tiled) + ByteTrack on the clip the SLAM ran on (stage 2 of tools/recon3d).

    YOLO_CONFIG_DIR=tools/recon3d/.venv/ultralytics-config tools/recon3d/.venv/bin/python tools/recon3d/detect.py <run>

The defaults are the strongest setting measured on the Labrador clip (YOLO26x fine-tuned on VisDrone, a 1280 px
full-frame pass, 640 px tiles at 1.5x and 2.0x, YOLO26x-seg masks on the boxes, ByteTrack tuned to keep small
low-confidence boxes): about 0.55 s per 1080p frame on an RTX 5090.

Reads <run>/clip.mp4, writes <run>/detections.json (or --out): per frame the boxes ByteTrack kept, each
with its 2D track id, COCO class, confidence, box (x1 y1 x2 y2, source pixels) and, where a segmentation
model saw it, the mask outline (polygon, source pixels, simplified). Only road users are kept; the
class ids downstream (lift.py) are COCO's: 0 person, 1 bicycle, 2 car, 3 motorcycle, 5 bus, 7 truck.
The 2D track id is the association; lift.py does everything in 3D.

Detector. `--model` is a path, a file in tools/recon3d/models/, or an Ultralytics name (downloaded, then
moved into models/). Classes are mapped by name, so COCO weights and the VisDrone fine-tunes both work:
pedestrian / people -> person, van -> car, tricycle / awning-tricycle / motor -> motorcycle, the rest
as in COCO; VisDrone's "others" is dropped. COCO models miss small aerial objects (docs/DETECTION_FINDINGS.md):
use a VisDrone fine-tune, and tiles for far objects.

Passes (each optional, boxes merged):
  full frame   `--imgsz N` (0 = none): the whole frame letterboxed to N px on its long side.
  tiles        `--tile T --overlap O --tile-scale S[,S2]` (T = 0: none): SAHI-style, tiles of T source px with
               overlap fraction O (evenly spaced, so every tile is full size and the overlap is at least O),
               each run at T*S px (S > 1 magnifies small objects; a comma list runs the grid once per scale),
               batched on the GPU.
  seg model    `--seg-model`: a COCO segmentation model run full frame only to put mask outlines on the
               final boxes (IoU >= 0.5, one mask per box; `none` = off). A seg `--model` gives masks directly.
               lift.py uses masks where a box has one and the box's centre window where it has not.

Merge. Class-agnostic NMS at `--merge-iou` over the whole boxes (one vehicle scored as both car and truck
is one box). A box a tile cut at an interior tile edge is an artefact of the seam: it is dropped when a
whole box explains it (IoU > merge-iou, or at least 70 % of it inside a whole box of its kind), and kept
only when nothing else saw that object (an object larger than the overlap). This is SAHI's seam merge
without growing boxes by union.

Tracking. Ultralytics' BYTETracker driven directly with bytetrack.yaml's settings; `--track-high/-low/
--new-track/--track-buffer/--match-thresh/--fuse-score` override them, `--conf` (default = track-low) is the
detector floor so the tracker's second (low-score) association sees its candidates. Notes from tuning
(runs/recon3d/labrador-det): fuse_score (cost = 1 - IoU * score) makes a box under ~0.25 unmatchable in the first
association, so with it on a track-high below 0.25 only fragments tracks and the tracked output loses 3-6 points
of recall against the raw boxes; off, 2-4. new-track only acts above track-high (a box starts a track
only after it passed track-high), so new-track < track-high is the same as equal. Boxes written are the
detector's own (not the Kalman-smoothed ones), so they match their masks. `--no-track` writes every box above
`--conf` with id -1 (for scoring single frames; lift.py needs real ids); `--retrack RAW.json` runs only the
tracker on such a file (tracker tuning without the detector). Prints seconds per frame (detect + merge + track,
decode excluded, one untimed warm-up call first).

The old run is `--model yolo11x-seg.pt --imgsz 1344 --tile 0 --seg-model none --conf 0.25 --track-high 0.25
--track-low 0.1 --new-track 0.25 --track-buffer 30 --fuse-score`.
`--frames` (e.g. `75,225` or `100-300` or `0-1199:2`) is for tests; lift.py needs every frame from 0.
"""
import argparse
import json
import pathlib
import shutil
import time

import cv2
import numpy as np
from ultralytics import YOLO
from ultralytics.trackers.byte_tracker import BYTETracker
from ultralytics.utils import YAML, IterableSimpleNamespace

KEEP = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
# Class name (COCO or VisDrone) -> COCO id. Anything else (VisDrone "others", COCO's 74 other classes) is dropped.
NAME_TO_COCO = {"person": 0, "pedestrian": 0, "people": 0, "bicycle": 1, "car": 2, "van": 2, "motorcycle": 3, "motor": 3,
                "tricycle": 3, "awning-tricycle": 3, "bus": 5, "truck": 7}
# Kinds that may sit inside each other's boxes (a rider on a motorcycle): the 70 % containment test of the seam merge
# only compares boxes of one kind.
KIND = {0: 0, 1: 1, 3: 1, 2: 2, 5: 2, 7: 2}
SEAM_MARGIN = 4.0   # a box this close (source px) to an interior tile edge was cut by it
CONTAIN = 0.7       # share of a cut box inside a whole box that explains it

ap = argparse.ArgumentParser()
ap.add_argument("run")
ap.add_argument("--model", default="visdrone-yolo26x.pt", help="path, file in tools/recon3d/models/, or an Ultralytics name")
ap.add_argument("--imgsz", type=int, default=1280, help="full-frame pass size on the long side (0: no full-frame pass)")
ap.add_argument("--tile", type=int, default=640, help="tile size in source px (0: no tiling)")
ap.add_argument("--overlap", type=float, default=0.2, help="tile overlap fraction")
ap.add_argument("--tile-scale", default="1.5,2.0", help="tile magnification: each tile is run at tile * scale px; a comma list runs the tiles once per scale")
ap.add_argument("--tile-batch", type=int, default=8, help="tiles per forward pass")
ap.add_argument("--seg-model", default="yolo26x-seg.pt", help="COCO segmentation model, full frame, for mask outlines on boxes without one ('none': off)")
ap.add_argument("--seg-imgsz", type=int, default=1920, help="its size (0: --imgsz, or 1344 without a full-frame pass)")
ap.add_argument("--seg-conf", type=float, default=0.2)
ap.add_argument("--conf", type=float, default=None, help="detector confidence floor (default: --track-low)")
ap.add_argument("--iou", type=float, default=0.7, help="the model's own NMS IoU, within one pass")
ap.add_argument("--merge-iou", type=float, default=0.5, help="IoU above which boxes of different passes/tiles are one object")
ap.add_argument("--track-high", type=float, default=0.25, help="ByteTrack track_high_thresh: first association")
ap.add_argument("--track-low", type=float, default=0.1, help="ByteTrack track_low_thresh: second association")
ap.add_argument("--new-track", type=float, default=0.25, help="ByteTrack new_track_thresh: a track starts from a box this confident (only boxes above --track-high get that far)")
ap.add_argument("--track-buffer", type=int, default=60, help="frames a lost track lives (60 = 2 s at 30 fps; bytetrack.yaml: 30)")
ap.add_argument("--match-thresh", type=float, default=None, help="ByteTrack match_thresh: max association cost (default: bytetrack.yaml, 0.8)")
ap.add_argument("--fuse-score", action="store_true", help="ByteTrack fuse_score on (bytetrack.yaml's default): the cost is 1 - IoU * score, so a box under ~0.25 "
                "cannot pass match_thresh 0.8 in the first association. Off here: tracks keep their low-confidence small boxes")
ap.add_argument("--no-track", action="store_true", help="raw boxes above --conf, id -1 (single-frame scoring)")
ap.add_argument("--frames", default=None, help="only these frames: comma list and ranges a-b[:step]")
ap.add_argument("--out", default=None, help="output path (default <run>/detections.json)")
ap.add_argument("--max-det", type=int, default=300, help="per forward pass and image")
ap.add_argument("--half", action="store_true", help="fp16 inference")
ap.add_argument("--e2e", action="store_true", help="use the NMS-free head of end-to-end models (YOLO26) instead of NMS")
ap.add_argument("--retrack", default=None, help="skip the detector: track the boxes of this --no-track detections.json (tracker tuning)")
args = ap.parse_args()

run = pathlib.Path(args.run)
models = pathlib.Path(__file__).parent / "models"
models.mkdir(exist_ok=True)
if args.conf is None:
    args.conf = args.track_low
if args.imgsz <= 0 and args.tile <= 0 and not args.retrack:
    raise SystemExit("nothing to run: --imgsz 0 and --tile 0")


def load(name):
    """A YOLO model from a path, from models/, or by Ultralytics name (downloaded into models/)."""
    for p in (pathlib.Path(name), models / name):
        if p.is_file():
            return YOLO(str(p))
    m = YOLO(name)   # downloads; ultralytics saves into the cwd / its weights dir: keep weights out of the repo tree
    src = pathlib.Path(str(m.ckpt_path)) if m.ckpt_path else None
    if src is not None and src.is_file() and src.resolve().parent != models.resolve():
        shutil.move(str(src), str(models / src.name))
    return m


class Detector:
    """One YOLO model: batches of images -> (xyxy, conf, COCO class, polygons or None) per image."""

    def __init__(self, name):
        self.model = load(name)
        names = self.model.names
        self.cmap = np.array([NAME_TO_COCO.get(str(names[i]).lower(), -1) for i in sorted(names)])
        self.ids = [int(i) for i in np.flatnonzero(self.cmap >= 0)]
        if not self.ids:
            raise SystemExit(f"{name}: no road-user class in {names}")
        self.seg = self.model.task == "segment"
        self.name = name

    def __call__(self, imgs, imgsz, conf):
        kw = dict(imgsz=imgsz, conf=conf, iou=args.iou, classes=self.ids, agnostic_nms=True, max_det=args.max_det,
                  retina_masks=self.seg, verbose=False)
        if args.half:
            kw["quantize"] = 16
        if args.e2e:
            kw["nms"] = False
        out = []
        for r in self.model.predict(imgs, **kw):
            b = r.boxes
            xyxy, cf, cl = b.xyxy.cpu().numpy().astype(np.float64), b.conf.cpu().numpy(), b.cls.cpu().numpy().astype(int)
            polys = list(r.masks.xy) if self.seg and r.masks is not None else None
            coco = self.cmap[cl]
            ok = coco >= 0
            if polys is not None:
                polys = [p for p, o in zip(polys, ok) if o]
            out.append((xyxy[ok], cf[ok], coco[ok], polys))
        return out


def tile_grid(w, h, t, overlap):
    """Evenly spaced tile origins covering w x h with tiles of t px (clamped to the frame) overlapping by >= overlap."""
    def starts(n):
        if n <= t:
            return [0], n
        o = overlap * t
        k = int(np.ceil((n - o) / (t - o)))
        return [int(round(v)) for v in np.linspace(0, n - t, k)], t
    xs, tw = starts(w)
    ys, th = starts(h)
    return [(x, y, tw, th) for y in ys for x in xs]


def box_inter(a, b):
    x1 = np.maximum(a[:, None, 0], b[None, :, 0]); y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2]); y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    return np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)


def box_area(b):
    return np.clip(b[:, 2] - b[:, 0], 0, None) * np.clip(b[:, 3] - b[:, 1], 0, None)


def iou_matrix(a, b):
    inter = box_inter(a, b)
    return inter / np.maximum(box_area(a)[:, None] + box_area(b)[None, :] - inter, 1e-9)


def merge(box, conf, cls, cut):
    """Indices kept: class-agnostic NMS over the whole boxes, then the seam-cut boxes nothing whole explains."""
    if len(box) == 0:
        return []
    order = np.argsort(-conf, kind="stable")
    inter = box_inter(box, box)
    area = box_area(box)
    iou = inter / np.maximum(area[:, None] + area[None, :] - inter, 1e-9)
    kind = np.array([KIND[c] for c in cls])
    keep = []
    for j in order:
        if not cut[j] and not any(iou[j, k] > args.merge_iou for k in keep):
            keep.append(int(j))
    for j in order:
        if cut[j] and not any(iou[j, k] > args.merge_iou or (kind[j] == kind[k] and inter[j, k] > CONTAIN * area[j]) for k in keep):
            keep.append(int(j))
    return keep


def simplify(poly):
    """Polygon (N, 2) -> rounded, simplified list of [x, y], None if it degenerates."""
    p = cv2.approxPolyDP(np.asarray(poly, np.float32).reshape(-1, 1, 2), 1.0, True).reshape(-1, 2)
    return [[round(float(x), 1), round(float(y), 1)] for x, y in p] if len(p) >= 3 else None


class Dets:
    """What BYTETracker.update reads: xyxy / xywh / conf / cls arrays that a boolean mask can index."""

    def __init__(self, xyxy, conf, cls):
        self.xyxy, self.conf, self.cls = xyxy, conf, cls

    @property
    def xywh(self):
        b = self.xyxy
        return np.stack([(b[:, 0] + b[:, 2]) / 2, (b[:, 1] + b[:, 3]) / 2, b[:, 2] - b[:, 0], b[:, 3] - b[:, 1]], 1)

    def __len__(self):
        return len(self.conf)

    def __getitem__(self, i):
        return Dets(self.xyxy[i], self.conf[i], self.cls[i])


def parse_frames(spec):
    if not spec:
        return None
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        rng, _, step = part.partition(":")
        a, _, b = rng.partition("-")
        out.update(range(int(a), int(b or a) + 1, int(step or 1)))
    return out


raw = json.loads(pathlib.Path(args.retrack).read_text()) if args.retrack else None
print(f"detect: model {args.model}, full frame {args.imgsz}, tiles {args.tile} x{args.tile_scale} overlap {args.overlap}, seg {args.seg_model}, conf {args.conf}, "
      f"track high/low/new {args.track_high}/{args.track_low}/{args.new_track} buffer {args.track_buffer} fuse_score {args.fuse_score}"
      + (", no tracker" if args.no_track else "") + (f", retrack {args.retrack}" if args.retrack else ""), flush=True)
det = Detector(args.model) if raw is None else None
seg = Detector(args.seg_model) if args.seg_model.lower() != "none" and raw is None else None
wanted = parse_frames(args.frames)
args.tile_scales = [float(v) for v in str(args.tile_scale).split(",")]
tracker = None
if not args.no_track:
    cfg = YAML.load(pathlib.Path(__import__("ultralytics").__file__).parent / "cfg" / "trackers" / "bytetrack.yaml")
    cfg.update(track_high_thresh=args.track_high, track_low_thresh=args.track_low, new_track_thresh=args.new_track)
    if args.track_buffer is not None:
        cfg["track_buffer"] = args.track_buffer
    if args.match_thresh is not None:
        cfg["match_thresh"] = args.match_thresh
    cfg["fuse_score"] = args.fuse_score
    tracker = BYTETracker(IterableSimpleNamespace(**cfg))
    if wanted is not None and sorted(wanted) != list(range(min(wanted), max(wanted) + 1)):
        print("warning: --frames is not a contiguous range; tracking across the gaps is meaningless (use --no-track)")


def detect(img):
    """Every pass, merged: xyxy (N, 4), conf, COCO class, polygons (list, None where unknown)."""
    H, W = img.shape[:2]
    box, conf, cls, cut, poly = [], [], [], [], []

    def add(xyxy, cf, cl, polys, off=(0, 0), seam=None):
        if len(cf) == 0:
            return
        o = np.array([off[0], off[1], off[0], off[1]])
        box.append(xyxy + o); conf.append(cf); cls.append(cl)
        cut.append(np.zeros(len(cf), bool) if seam is None else seam)
        poly.extend([None] * len(cf) if polys is None else [p + o[:2] for p in polys])

    if args.imgsz > 0:
        (xyxy, cf, cl, polys), = det([img], args.imgsz, args.conf)
        add(xyxy, cf, cl, polys)
    if args.tile > 0:
        grid = tile_grid(W, H, args.tile, args.overlap)
        for scale in args.tile_scales:
            isz = int(round(args.tile * scale / 32)) * 32
            for s in range(0, len(grid), args.tile_batch):
                chunk = grid[s:s + args.tile_batch]
                crops = [np.ascontiguousarray(img[y:y + th, x:x + tw]) for x, y, tw, th in chunk]
                for (x, y, tw, th), (xyxy, cf, cl, polys) in zip(chunk, det(crops, isz, args.conf)):
                    m = SEAM_MARGIN
                    seam = ((xyxy[:, 0] < m) & (x > 0)) | ((xyxy[:, 1] < m) & (y > 0)) | ((xyxy[:, 2] > tw - m) & (x + tw < W)) | ((xyxy[:, 3] > th - m) & (y + th < H))
                    add(xyxy, cf, cl, polys, (x, y), seam)
    if not box:
        return np.zeros((0, 4)), np.zeros(0), np.zeros(0, int), []
    box, conf, cls, cut = np.concatenate(box), np.concatenate(conf), np.concatenate(cls), np.concatenate(cut)
    keep = merge(box, conf, cls, cut)
    box, conf, cls = box[keep], conf[keep], cls[keep]
    poly = [None if poly[k] is None or len(poly[k]) < 3 else simplify(poly[k]) for k in keep]
    if seg is not None and len(box):
        # Mask outlines for the boxes that have none: the seg model's instance with the best IoU >= 0.5, one box each.
        sz = args.seg_imgsz or args.imgsz or 1344
        (sx, sc, scl, spolys), = seg([img], sz, args.seg_conf)
        if len(sx) and spolys is not None:
            iou = iou_matrix(box, sx)
            for k in range(len(box)):
                if poly[k] is not None:
                    iou[k] = 0
            while True:
                k, m = np.unravel_index(np.argmax(iou), iou.shape)
                if iou[k, m] < 0.5:
                    break
                poly[k] = simplify(spolys[m]) if len(spolys[m]) >= 3 else None
                iou[k, :] = 0; iou[:, m] = 0
    return box, conf, cls, poly


def to_frame(i, box, conf, cls, poly, ident):
    dets = []
    for k in range(len(box)):
        d = {"id": int(ident[k]), "cls": int(cls[k]), "conf": round(float(conf[k]), 3), "box": [round(float(v), 1) for v in box[k]]}
        if poly[k] is not None:
            d["poly"] = poly[k]
        dets.append(d)
    return {"i": i, "dets": dets}


def from_raw(f):
    """The boxes of one frame of a --no-track file above --conf, as detect() returns them."""
    ds = [d for d in f["dets"] if d["conf"] >= args.conf]
    return (np.array([d["box"] for d in ds], float).reshape(-1, 4), np.array([d["conf"] for d in ds], float),
            np.array([d["cls"] for d in ds], int), [d.get("poly") for d in ds])


def sources():
    """(frame index, image or the raw frame) for the frames wanted."""
    if raw is not None:
        for f in raw["frames"]:
            if wanted is None or f["i"] in wanted:
                yield f["i"], f
        return
    cap = cv2.VideoCapture(str(run / "clip.mp4"))
    i = -1
    last = max(wanted) if wanted else None
    while last is None or i < last:
        i += 1
        if wanted is not None and i not in wanted:
            if not cap.grab():
                return
            continue
        ok, img = cap.read()
        if not ok:
            return
        yield i, img


frames, times = [], []
for i, src in sources():
    if raw is None and not times and not frames:
        detect(src)   # warm-up (CUDA init, cudnn): not timed
    t0 = time.perf_counter()
    box, conf, cls, poly = detect(src) if raw is None else from_raw(src)
    if tracker is not None:
        tr = tracker.update(Dets(box, conf.astype(np.float64), cls.astype(np.float64)))
        idx = tr[:, 7].astype(int) if len(tr) else np.zeros(0, int)
        f = to_frame(i, box[idx], conf[idx], cls[idx], [poly[k] for k in idx], tr[:, 4].astype(int) if len(tr) else [])
    else:
        f = to_frame(i, box, conf, cls, poly, -np.ones(len(box), int))
    times.append(time.perf_counter() - t0)
    frames.append(f)
    if len(frames) % 50 == 1:
        print(f"frame {i}: {len(f['dets'])} boxes, {np.mean(times):.3f} s/frame", flush=True)

src_info = {k: raw.get(k) for k in ("model", "imgsz", "tile", "overlap", "tile_scale", "seg_model")} if raw is not None else \
    {"model": args.model, "imgsz": args.imgsz, "tile": args.tile, "overlap": args.overlap, "tile_scale": args.tile_scale, "seg_model": args.seg_model if seg is not None else None}
out = {**src_info, "conf": args.conf, "tracked": tracker is not None, "retracked_from": args.retrack,
       "track": {"high": args.track_high, "low": args.track_low, "new": args.new_track, "buffer": tracker.args.track_buffer if tracker else None,
                 "match": tracker.args.match_thresh if tracker else None, "fuse_score": tracker.args.fuse_score if tracker else None},
       "classes": KEEP, "s_per_frame": round(float(np.mean(times)) + (raw.get("s_per_frame") or 0.0 if raw is not None else 0.0), 4) if times else None,
       "frames": frames}
path = pathlib.Path(args.out) if args.out else run / "detections.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(out))
ids = {d["id"] for f in frames for d in f["dets"]}
n = sum(len(f["dets"]) for f in frames)
print(f"{len(frames)} frames, {n} boxes ({n / max(1, len(frames)):.1f}/frame), {len(ids) if tracker is not None else 0} 2D tracks, "
      f"{np.mean(times) if times else 0:.3f} s/frame -> {path}")
