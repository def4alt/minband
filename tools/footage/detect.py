"""YOLO (ONNX) on tiles of a large frame, for small objects in drone footage.

A 4K drone frame shrunk to the detector's 640 px input loses people entirely, so the frame is cut
into overlapping tiles, each tile is run at the network's input size, and the boxes are merged with
class-wise NMS. Only the classes MinBand tracks from the air are kept, mapped to MinBand's ids by the
class names the export carries (COCO, VisDrone and military-vehicle models all work).
"""
import ast
import numpy as np
import cv2
import onnxruntime as ort

# MinBand ids kept: COCO person, bicycle, car, motorcycle, bus, truck, and MinBand's armoured vehicle.
KEEP = {0: 'person', 1: 'bicycle', 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck', 101: 'armoured'}
ARMOURED = 101
# Model class names -> MinBand ids. Soldiers stay dismounts (person), military trucks stay trucks; an
# armoured class (101) only where the model says tank / armoured vehicle. Anything else (weapons,
# trenches, aircraft, ships, artillery pieces) is not tracked.
NAME_MAP = {
    'person': 0, 'pedestrian': 0, 'people': 0, 'soldier': 0, 'camouflage_soldier': 0, 'civilian': 0,
    'military_personnel': 0, 'personnel': 0,
    'bicycle': 1,
    'car': 2, 'van': 2, 'civilian_vehicle': 2, 'hummer': 2, 'humvee': 2,
    'motorcycle': 3, 'motor': 3, 'tricycle': 3, 'awning-tricycle': 3,
    'bus': 5,
    'truck': 7, 'military_truck': 7,
    'tank': ARMOURED, 'military_tank': ARMOURED, 'armored_vehicle': ARMOURED, 'armoured_vehicle': ARMOURED,
    'military_vehicle': ARMOURED, 'apc': ARMOURED, 'ifv': ARMOURED,
}
# A VisDrone-trained model's ten classes, mapped to those COCO ids (for an export without names).
VISDRONE_TO_COCO = {0: 0, 1: 0, 2: 1, 3: 2, 4: 2, 5: 7, 6: 3, 7: 3, 8: 5, 9: 3}


def classmap_from_meta(meta: dict):
    """Model class index -> MinBand id (-1 = not tracked), from the export's `names`; None = COCO ids as-is."""
    names = meta.get('names')
    if names:
        try:
            d = ast.literal_eval(names)
            return {int(i): NAME_MAP.get(str(n).strip().lower(), -1) for i, n in d.items()}
        except (ValueError, SyntaxError):
            pass
    return VISDRONE_TO_COCO if 'visdrone' in meta.get('description', '').lower() else None


def nms(d: np.ndarray, conf: float, iou: float = 0.5) -> np.ndarray:
    """Class-wise NMS over N x 6 rows (x1 y1 x2 y2 conf cls)."""
    if not len(d): return d
    keep = []
    for k in np.unique(d[:, 5]):
        idx = np.where(d[:, 5] == k)[0]
        # NMSBoxes takes x, y, w, h: passing x2, y2 as the size made every box as large as its own
        # coordinates, so neighbours a few metres apart (people in a group) suppressed each other.
        xywh = np.column_stack([d[idx, 0], d[idx, 1], d[idx, 2] - d[idx, 0], d[idx, 3] - d[idx, 1]])
        sel = cv2.dnn.NMSBoxes(xywh.tolist(), d[idx, 4].tolist(), conf, iou)
        keep.extend(idx[np.array(sel, int).reshape(-1)])
    return d[sorted(keep)]


class TiledDetector:
    def __init__(self, model_path: str, tile: int = 960, overlap: int = 96, conf: float = 0.25, threads: int = 0, size: int = 0):
        so = ort.SessionOptions()
        if threads: so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(model_path, so, providers=['CPUExecutionProvider'])
        self.inp = self.sess.get_inputs()[0].name
        meta = self.sess.get_modelmeta().custom_metadata_map
        self.end2end = meta.get('end2end') == 'True'  # N x 6 boxes, NMS done in the graph
        self.classmap = classmap_from_meta(meta)
        self.names = meta.get('names', '')
        dim = self.sess.get_inputs()[0].shape[2]
        # Fixed exports take their own size; a dynamic one runs each tile at its own resolution
        # (rounded to the stride), so small objects are not shrunk at all.
        self.fixed = isinstance(dim, int)
        self.size = size or (dim if self.fixed else 0)  # 0: each tile at its own size, rounded to the stride
        self.tile, self.overlap, self.conf = tile, overlap, conf

    def tile_for(self, w: int, h: int) -> int:
        """Tile side in frame pixels. tile=0 (auto, resolution-aware): a fixed magnification of 1.5x
        into the network input (640 px tiles for a 960 input, the setting tuned on 4K MEVA), so an
        object of a given pixel size meets the detector at the same size whatever the frame size:
        4K gets 28 tiles, 1080p 8, 720p 4, and a frame smaller than a tile one tile, padded (not
        stretched). Smaller tiles on low-resolution dev clips (2-3x) found fewer vehicles and more
        clutter (README, battlefield section)."""
        return self.tile or int(round((self.size or 960) / 1.5))

    def tiles(self, w: int, h: int):
        tile = self.tile_for(w, h)
        step = max(1, tile - self.overlap)
        xs = list(range(0, max(1, w - self.overlap), step)); ys = list(range(0, max(1, h - self.overlap), step))
        for y in ys:
            for x in xs:
                x0, y0 = min(x, max(0, w - tile)), min(y, max(0, h - tile))
                yield x0, y0, min(tile, w), min(tile, h)

    def __call__(self, frame: np.ndarray, raw: bool = False):
        """N x 6: x1, y1, x2, y2, conf, cls (MinBand id) in frame pixels, after class-wise NMS.
        With raw=True, (after NMS, before NMS): the pre-NMS rows let a cache redo the merge."""
        h, w = frame.shape[:2]
        tile = self.tile_for(w, h)
        size = self.size or (tile + 31) // 32 * 32
        crops, origins = [], []
        for x0, y0, tw, th in dict.fromkeys(self.tiles(w, h)):
            crop = frame[y0:y0 + th, x0:x0 + tw]
            if tw != tile or th != tile:  # frame smaller than a tile: pad (same magnification), do not stretch
                pad = np.full((tile, tile, 3), 114, np.uint8); pad[:th, :tw] = crop; crop = pad
                tw = th = tile
            crops.append(cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA))
            origins.append((x0, y0, tw / size, th / size))
        batch = np.stack(crops)[..., ::-1].transpose(0, 3, 1, 2).astype(np.float32) / 255.0  # BGR -> RGB, NCHW
        out = []
        for i in range(len(crops)):  # one tile per run: the stock export's batch dimension is 1
            pred = self.sess.run(None, {self.inp: batch[i:i + 1]})[0][0]
            x0, y0, sx, sy = origins[i]
            if self.end2end:  # 300 x 6: x1 y1 x2 y2 conf cls
                b, c, k = pred[:, :4], pred[:, 4], pred[:, 5].astype(int)
                xyxy = b * [sx, sy, sx, sy] + [x0, y0, x0, y0]
            else:  # (4 + classes) x anchors: cx cy w h, then class scores
                boxes, scores = pred[:4].T, pred[4:].T
                k = scores.argmax(1); c = scores[np.arange(len(k)), k]; b = boxes
                xyxy = np.stack([(b[:, 0] - b[:, 2] / 2) * sx + x0, (b[:, 1] - b[:, 3] / 2) * sy + y0,
                                 (b[:, 0] + b[:, 2] / 2) * sx + x0, (b[:, 1] + b[:, 3] / 2) * sy + y0], 1)
            if self.classmap is not None: k = np.array([self.classmap.get(int(x), -1) for x in k])
            m = (c >= self.conf) & np.isin(k, list(KEEP))
            if m.any(): out.append(np.column_stack([xyxy[m], c[m], k[m]]))
        d = np.concatenate(out) if out else np.zeros((0, 6), np.float32)
        kept = nms(d, self.conf)
        return (kept, d) if raw else kept
