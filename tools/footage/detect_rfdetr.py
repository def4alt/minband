"""RF-DETR (PyTorch, CPU) on tiles of the detection frames, written as a normal detection cache.

  python detect_rfdetr.py VIDEO --weights models/rfdetr-dl/visdrone-rfdetr-nano.pth --tag rfdetr_vd --out DIR \
      [--size nano|small|medium] [--frames 150,300,600,900] [--tile 640] [--conf 0.15]

Same tiling as detect.py (tile side in frame pixels, 64 px overlap, class-wise NMS), the model's own
class names mapped to MinBand ids with detect.NAME_MAP. Without --frames, every frame of DIR/detect.json
(slow on CPU: use --frames for an audit). Weights trained on VisDrone are non-commercial (CC BY-NC-SA 3.0
dataset licence); the rfdetr package itself is Apache-2.0.
"""
import argparse, json, os, sys, time
import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from detect import NAME_MAP, KEEP, nms  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument('video'); p.add_argument('--weights', required=True); p.add_argument('--tag', required=True); p.add_argument('--out', required=True)
    p.add_argument('--size', default='nano'); p.add_argument('--frames'); p.add_argument('--tile', type=int, default=640)
    p.add_argument('--conf', type=float, default=0.15); p.add_argument('--names', help='class names JSON list (default: checkpoint config)')
    a = p.parse_args()
    import rfdetr
    cls = {'nano': rfdetr.RFDETRNano, 'small': rfdetr.RFDETRSmall, 'medium': rfdetr.RFDETRMedium}[a.size]
    model = cls(pretrain_weights=a.weights, device='cpu')
    names = json.load(open(a.names)) if a.names else getattr(model, 'class_names', None) or json.load(open(os.path.join(os.path.dirname(a.weights), 'config.json'))).get('class_names')
    if isinstance(names, dict): names = [names[k] for k in sorted(names, key=int)]
    print('names', names, flush=True)
    cmap = {i: NAME_MAP.get(str(n).strip().lower(), -1) for i, n in enumerate(names)}
    meta = json.load(open(os.path.join(a.out, 'detect.json')))
    frames = [int(x) for x in a.frames.split(',')] if a.frames else sorted(int(k) for k in meta['homographies'])
    cap = cv2.VideoCapture(a.video); rows = []; t0 = time.time()
    for n in frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n); ok, fr = cap.read()
        if not ok: break
        h, w = fr.shape[:2]; tile = a.tile; step = tile - 64; out = []
        xs = list(range(0, max(1, w - 64), step)); ys = list(range(0, max(1, h - 64), step))
        for y in ys:
            for x in xs:
                x0, y0 = min(x, max(0, w - tile)), min(y, max(0, h - tile))
                crop = np.ascontiguousarray(fr[y0:y0 + tile, x0:x0 + tile, ::-1])
                det = model.predict(crop, threshold=a.conf)
                if len(det.xyxy) == 0: continue
                k = np.array([cmap.get(int(c), -1) for c in det.class_id])
                m = np.isin(k, list(KEEP))
                if m.any(): out.append(np.column_stack([det.xyxy[m] + [x0, y0, x0, y0], det.confidence[m], k[m]]))
        d = nms(np.concatenate(out), a.conf) if out else np.zeros((0, 6))
        for x1, y1, x2, y2, c, k in d: rows.append([n, x1, y1, x2, y2, c, k])
        print(f'frame {n}: {len(d)} boxes ({(time.time() - t0) / (frames.index(n) + 1):.1f} s/frame)', flush=True)
    np.save(os.path.join(a.out, f'detections-{a.tag}.npy'), np.array(rows, np.float64).reshape(-1, 7))
    meta.update({'model': f'rfdetr-{a.size} {os.path.basename(a.weights)}', 'names': str(dict(enumerate(names))), 'tile': a.tile, 'conf': a.conf,
                 'homographies': {str(n): meta['homographies'][str(n)] for n in frames if str(n) in meta['homographies']}})
    json.dump(meta, open(os.path.join(a.out, f'detect-{a.tag}.json'), 'w'))


if __name__ == '__main__':
    main()
