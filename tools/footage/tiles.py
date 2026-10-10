"""Eye audit of a tracking run: one row per reported track (three crops at 20/50/80 % of its
detections, labelled id, class, seconds) and full frames with every reported box drawn.

    .venv/bin/python -I tiles.py RUN_DIR CLIP.mp4 --out PREFIX [--frames 4]

Writes PREFIX-tiles.jpg and PREFIX-frame-N.jpg. For unlicensed clips write to a scratch dir:
frames of those clips never go in the repo."""
import argparse, csv, os
import cv2
import numpy as np

NAMES = {0: 'person', 1: 'person', 2: 'car', 3: 'van', 4: 'van', 5: 'truck', 7: 'truck', 8: 'bus', 9: 'moto', 100: 'mover', 101: 'armour'}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('run'); ap.add_argument('clip'); ap.add_argument('--out', required=True)
    ap.add_argument('--frames', type=int, default=4); ap.add_argument('--size', type=int, default=112)
    a = ap.parse_args()
    reported = set()
    with open(os.path.join(a.run, 'tracks.csv')) as fh:
        for r in csv.reader(fh):
            if r and r[0][0].isdigit(): reported.add(int(r[1]))
    log = np.load(os.path.join(a.run, 'detlog.npy'))
    log = log[(log[:, 8] > 0) & np.isin(log[:, 8], list(reported))]
    cap = cv2.VideoCapture(a.clip)
    def frame(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n)); ok, im = cap.read(); return im if ok else None
    rows = []
    for tid in sorted(reported):
        d = log[log[:, 8] == tid]
        if not len(d): continue
        d = d[np.argsort(d[:, 0])]
        cls = int(np.bincount(d[:, 2].astype(int)).argmax())
        tiles = []
        for q in (0.2, 0.5, 0.8):
            r = d[min(len(d) - 1, int(q * len(d)))]
            im = frame(r[0])
            if im is None: continue
            x1, y1, x2, y2 = r[4:8]; cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            s = max(x2 - x1, y2 - y1) * 1.8 + 16
            X1, Y1 = int(max(0, cx - s / 2)), int(max(0, cy - s / 2)); X2, Y2 = int(min(im.shape[1], cx + s / 2)), int(min(im.shape[0], cy + s / 2))
            c = im[Y1:Y2, X1:X2].copy()
            cv2.rectangle(c, (int(x1 - X1), int(y1 - Y1)), (int(x2 - X1), int(y2 - Y1)), (0, 255, 255), 1)
            tiles.append(cv2.resize(c, (a.size, a.size)))
        while len(tiles) < 3: tiles.append(np.zeros((a.size, a.size, 3), np.uint8))
        lab = np.zeros((a.size, 150, 3), np.uint8)
        cv2.putText(lab, f'#{tid} {NAMES.get(cls, cls)}', (6, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(lab, f'{d[0, 9]:.1f} s', (6, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        rows.append(np.hstack([lab, *tiles]))
    if rows: cv2.imwrite(a.out + '-tiles.jpg', np.vstack(rows))
    nmin, nmax = int(log[:, 0].min()) if len(log) else 0, int(log[:, 0].max()) if len(log) else 0
    for i, n in enumerate(np.linspace(nmin, nmax, a.frames).astype(int)):
        im = frame(n)
        if im is None: continue
        near = log[np.abs(log[:, 0] - n) <= 2]
        for r in near:
            x1, y1, x2, y2 = map(int, r[4:8])
            cv2.rectangle(im, (x1, y1), (x2, y2), (0, 255, 255), 2)
            cv2.putText(im, f'#{int(r[8])} {NAMES.get(int(r[2]), int(r[2]))}', (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        cv2.imwrite(f'{a.out}-frame-{i}.jpg', im)
    print(f'{len(rows)} tracks; frames {nmin}..{nmax}')

if __name__ == '__main__':
    main()
