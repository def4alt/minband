"""Cross-tile merge self-check for ClikaTiledDetector -- no model load, no network.

Stubs DetectionModel.detect() to report one fixed-world-position box, computed in
each tile's own local coordinates by walking the same origin sequence __call__ uses.
That box sits in the overlap strip shared by several tiles, so it gets reported more
than once; nms() must collapse those duplicates into the single kept box.
"""
import numpy as np

from detector import ClikaTiledDetector


class _StubDetection:
    def __init__(self, xmin, ymin, xmax, ymax, score, label_id):
        self.xmin, self.ymin, self.xmax, self.ymax = xmin, ymin, xmax, ymax
        self.score, self.label_id = score, label_id


class _StubDetections:
    def __init__(self, items):
        self.items = items


def test_cross_tile_merge():
    det = ClikaTiledDetector.__new__(ClikaTiledDetector)
    det.classmap = {0: 0}  # label_id 0 -> MinBand "person"
    det.fixed, det.size = 0, 0
    det.tile, det.overlap, det.conf = 400, 96, 0.25

    w, h = 1200, 480
    box = (340, 100, 380, 300)  # full-frame world box, inside tile overlap zone
    origins = iter(dict.fromkeys(det.tiles(w, h)))

    class StubModel:
        labels = ["person"]

        def detect(self, tensor, confidence, max_detections):
            x0, y0, tw, th = next(origins)
            bx0, by0, bx1, by1 = box
            if x0 <= bx0 and bx1 <= x0 + tw and y0 <= by0 and by1 <= y0 + th:
                return _StubDetections([_StubDetection(bx0 - x0, by0 - y0, bx1 - x0, by1 - y0, 0.9, 0)])
            return _StubDetections([])

    det.model = StubModel()
    frame = np.full((h, w, 3), 114, np.uint8)
    kept = det(frame)
    assert len(kept) == 1, f"expected one merged box from overlapping tiles, got {len(kept)}"


if __name__ == "__main__":
    test_cross_tile_merge()
    print("ok")
