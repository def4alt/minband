"""YOLO11 (ONNX, COCO) on tiles of a large frame, for small objects in drone footage.

A 4K drone frame shrunk to the detector's 640 px input loses people entirely, so the frame is cut
into overlapping tiles, each tile is run at the network's input size, and the boxes are merged with
class-wise NMS. Only the classes MinBand tracks from the air are kept.
"""
import numpy as np
import cv2
import onnxruntime as ort

# COCO ids kept: person, bicycle, car, motorcycle, bus, truck. MinBand's class ids are COCO's.
KEEP = {0: 'person', 1: 'bicycle', 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck'}
# A VisDrone-trained model's ten classes, mapped to those COCO ids.
VISDRONE_TO_COCO = {0: 0, 1: 0, 2: 1, 3: 2, 4: 2, 5: 7, 6: 3, 7: 3, 8: 5, 9: 3}


class TiledDetector:
    def __init__(self, model_path: str, tile: int = 960, overlap: int = 96, conf: float = 0.25, threads: int = 0, size: int = 0):
        so = ort.SessionOptions()
        if threads: so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(model_path, so, providers=['CPUExecutionProvider'])
        self.inp = self.sess.get_inputs()[0].name
        meta = self.sess.get_modelmeta().custom_metadata_map
        self.end2end = meta.get('end2end') == 'True'  # N x 6 boxes, NMS done in the graph
        self.classmap = VISDRONE_TO_COCO if 'visdrone' in meta.get('description', '').lower() else None
        dim = self.sess.get_inputs()[0].shape[2]
        # Fixed exports take their own size; a dynamic one runs each tile at its own resolution
        # (rounded to the stride), so small objects are not shrunk at all.
        self.size = size or (dim if isinstance(dim, int) else (tile + 31) // 32 * 32)
        self.tile, self.overlap, self.conf = tile, overlap, conf

    def tiles(self, w: int, h: int):
        step = self.tile - self.overlap
        xs = list(range(0, max(1, w - self.overlap), step)); ys = list(range(0, max(1, h - self.overlap), step))
        for y in ys:
            for x in xs:
                x0, y0 = min(x, max(0, w - self.tile)), min(y, max(0, h - self.tile))
                yield x0, y0, min(self.tile, w), min(self.tile, h)

    def __call__(self, frame: np.ndarray) -> np.ndarray:
        """Returns N x 6: x1, y1, x2, y2, conf, cls in frame pixels."""
        h, w = frame.shape[:2]
        crops, origins = [], []
        for x0, y0, tw, th in dict.fromkeys(self.tiles(w, h)):
            crop = frame[y0:y0 + th, x0:x0 + tw]
            crops.append(cv2.resize(crop, (self.size, self.size), interpolation=cv2.INTER_AREA))
            origins.append((x0, y0, tw / self.size, th / self.size))
        batch = np.stack(crops)[..., ::-1].transpose(0, 3, 1, 2).astype(np.float32) / 255.0  # BGR -> RGB, NCHW
        out = []
        for i in range(len(crops)):  # one tile per run: the stock export's batch dimension is 1
            pred = self.sess.run(None, {self.inp: batch[i:i + 1]})[0][0]
            x0, y0, sx, sy = origins[i]
            if self.end2end:  # 300 x 6: x1 y1 x2 y2 conf cls
                b, c, k = pred[:, :4], pred[:, 4], pred[:, 5].astype(int)
                xyxy = b * [sx, sy, sx, sy] + [x0, y0, x0, y0]
            else:  # 84 x anchors: cx cy w h, then class scores
                boxes, scores = pred[:4].T, pred[4:].T
                k = scores.argmax(1); c = scores[np.arange(len(k)), k]; b = boxes
                xyxy = np.stack([(b[:, 0] - b[:, 2] / 2) * sx + x0, (b[:, 1] - b[:, 3] / 2) * sy + y0,
                                 (b[:, 0] + b[:, 2] / 2) * sx + x0, (b[:, 1] + b[:, 3] / 2) * sy + y0], 1)
            if self.classmap is not None: k = np.array([self.classmap.get(int(x), -1) for x in k])
            m = (c >= self.conf) & np.isin(k, list(KEEP))
            if m.any(): out.append(np.column_stack([xyxy[m], c[m], k[m]]))
        if not out: return np.zeros((0, 6), np.float32)
        d = np.concatenate(out)
        keep = []
        for k in np.unique(d[:, 5]):
            idx = np.where(d[:, 5] == k)[0]
            sel = cv2.dnn.NMSBoxes(d[idx, :4].tolist(), d[idx, 4].tolist(), self.conf, 0.5)
            keep.extend(idx[np.array(sel).reshape(-1)])
        return d[sorted(keep)]
