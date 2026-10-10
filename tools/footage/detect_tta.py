"""Horizontal-flip test-time augmentation for the tiled detector, written as a normal detection cache.

  python detect_tta.py VIDEO --model M.onnx --tag vd26s_tta --out DIR [--tile 0] [--conf 0.15] [--mode union|and]

Runs detect.py's TiledDetector on each detection frame of DIR/detect.json (same frames as the other
caches; the homographies are copied, so no registration is redone) and on the frame mirrored
left-right, un-mirrors the second set and merges: `union` (default) keeps everything after class-wise
NMS (the box seen in either pass, recall up), `and` keeps only boxes the mirrored pass confirms
(IoU >= 0.3, any class; precision up). Output: detections-TAG.npy and detect-TAG.json.
"""
import argparse, json, os, sys, time
import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from detect import TiledDetector, nms  # noqa: E402
from detect_eval import iou_matrix  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument('video'); p.add_argument('--model', required=True); p.add_argument('--tag', required=True); p.add_argument('--out', required=True)
    p.add_argument('--tile', type=int, default=0); p.add_argument('--conf', type=float, default=0.15)
    p.add_argument('--mode', default='union', choices=['union', 'and']); p.add_argument('--iou', type=float, default=0.3)
    p.add_argument('--base', default='detect.json', help='cache meta whose frames and homographies are reused')
    p.add_argument('--frames', help='only these frames (comma list), e.g. for an audit')
    a = p.parse_args()
    meta = json.load(open(os.path.join(a.out, a.base)))
    frames = [int(x) for x in a.frames.split(',')] if a.frames else sorted(int(k) for k in meta['homographies'])
    det = TiledDetector(a.model, tile=a.tile, overlap=64, conf=a.conf)
    cap = cv2.VideoCapture(a.video); rows = []; t0 = time.time()
    for n in frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n); ok, fr = cap.read()
        if not ok: break
        w = fr.shape[1]
        d1 = det(fr); d2 = det(fr[:, ::-1].copy())
        if len(d2): d2[:, [0, 2]] = w - d2[:, [2, 0]]
        if a.mode == 'union':
            d = nms(np.concatenate([d1, d2]), a.conf) if len(d1) + len(d2) else d1
        else:
            m = iou_matrix(d1[:, :4], d2[:, :4]).max(1) >= a.iou if len(d1) and len(d2) else np.zeros(len(d1), bool)
            d = d1[m]
        for x1, y1, x2, y2, c, k in d: rows.append([n, x1, y1, x2, y2, c, k])
        print(f'frame {n}: {len(d1)} + {len(d2)} flipped -> {len(d)} ({a.mode})', flush=True)
    np.save(os.path.join(a.out, f'detections-{a.tag}.npy'), np.array(rows, np.float64).reshape(-1, 7))
    meta.update({'model': f'{os.path.basename(a.model)} +hflip TTA ({a.mode})', 'names': det.names, 'tile': det.tile_for(w, fr.shape[0]), 'conf': a.conf})
    json.dump(meta, open(os.path.join(a.out, f'detect-{a.tag}.json'), 'w'))
    print(f'{len(frames)} frames in {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main()
