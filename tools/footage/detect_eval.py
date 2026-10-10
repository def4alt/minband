"""Eye-audit helpers for detection caches (detections-<tag>.npy, rows frame x1 y1 x2 y2 conf cls).

  python detect_eval.py draw  RUN TAG [TAG ...] --video V --frames 150,600,900 --out A   # boxes on raw frames
  python detect_eval.py stats RUN TAG [TAG ...]                                        # boxes / frame, classes, conf
  python detect_eval.py consensus RUN A B --iou 0.3 --out TAG [--mode and|union]       # two-detector agreement cache
  python detect_eval.py diff  RUN A B --frames 600,900 [--iou 0.3]                      # boxes of A with no match in B
  python detect_eval.py score RUN TAG [TAG ...] --inventory audit-inventory-<clip>.json [--conf 0.15]  # true / false per frame

`score` counts, per audit frame, the inventory objects with at least one box (any class) on them,
the boxes on nothing (false), and the extra boxes on an already-counted object (dup); boxes whose
centre falls in an ignore region are skipped. A box is on an object when its IoU with the object's
box is >= 0.1 or it contains the object's centre.

There is no ground truth: `draw` is for looking (Read the jpg and count true / false boxes by eye);
`stats` and `diff` make the counting quicker. `consensus` keeps a box of A when any box of B
overlaps it by IoU >= --iou (any class; class and conf come from A), mode `union` also adds the
matched boxes of B that A does not have (class-wise NMS, IoU 0.5), so a detector pair acts as one
detector with the precision of the agreement and the recall of either. The output is a normal
cache (detections-TAG.npy + detect-TAG.json copied from A) that track.py can use as a source.
"""
import argparse, json, os, shutil
import numpy as np
import cv2

NAMES = {0: 'person', 1: 'bicycle', 2: 'car', 3: 'moto', 5: 'bus', 7: 'truck', 101: 'armoured'}
COLORS = {0: (0, 255, 255), 1: (255, 128, 0), 2: (0, 255, 0), 3: (255, 128, 0), 5: (255, 0, 255), 7: (255, 0, 255), 101: (0, 0, 255)}


def cache(run, tag):
    return os.path.join(run, 'detections.npy' if tag == 'det' else f'detections-{tag}.npy')


def meta(run, tag):
    return os.path.join(run, 'detect.json' if tag == 'det' else f'detect-{tag}.json')


def load(run, tag):
    return np.load(cache(run, tag)).reshape(-1, 7)


def iou_matrix(a, b):
    """a: N x 4, b: M x 4 (x1 y1 x2 y2) -> N x M IoU."""
    if not len(a) or not len(b): return np.zeros((len(a), len(b)))
    x1 = np.maximum(a[:, None, 0], b[None, :, 0]); y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2]); y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    aa = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1]); ab = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(aa[:, None] + ab[None, :] - inter, 1e-9)


def nms(d, thr=0.5):
    """Class-wise NMS on N x 7 rows (frame x1 y1 x2 y2 conf cls), one frame."""
    keep = []
    for k in np.unique(d[:, 6]):
        idx = np.where(d[:, 6] == k)[0]
        xywh = np.column_stack([d[idx, 1], d[idx, 2], d[idx, 3] - d[idx, 1], d[idx, 4] - d[idx, 2]])
        sel = cv2.dnn.NMSBoxes(xywh.tolist(), d[idx, 5].tolist(), 0.0, thr)
        keep.extend(idx[np.array(sel, int).reshape(-1)])
    return d[sorted(keep)]


def frame_at(video, n):
    cap = cv2.VideoCapture(video); cap.set(cv2.CAP_PROP_POS_FRAMES, n)
    ok, f = cap.read(); cap.release()
    if not ok: raise SystemExit(f'cannot read frame {n}')
    return f


def draw(img, rows, thick=2):
    for _, x1, y1, x2, y2, c, k in rows:
        col = COLORS.get(int(k), (255, 255, 255))
        if c < 0.3: col = tuple(int(v * 0.55) for v in col)
        cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), col, thick)
        cv2.putText(img, f'{NAMES.get(int(k), int(k))} {c:.2f}', (int(x1), max(12, int(y1) - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
    return img


def cmd_draw(a):
    frames = [int(x) for x in a.frames.split(',')]
    os.makedirs(a.out, exist_ok=True)
    for tag in a.tags:
        d = load(a.run, tag)
        for n in frames:
            rows = d[d[:, 0] == n]
            img = draw(frame_at(a.video, n), rows)
            cv2.putText(img, f'{tag} f{n}: {len(rows)} boxes', (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
            p = os.path.join(a.out, f'{tag}-{n}.jpg'); cv2.imwrite(p, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            print(p, len(rows), 'boxes:', ', '.join(f'{NAMES.get(int(k), int(k))}@{int(x1)},{int(y1)} {c:.2f}' for _, x1, y1, x2, y2, c, k in rows))


def cmd_stats(a):
    for tag in a.tags:
        d = load(a.run, tag)
        frames = np.unique(d[:, 0]); nf = len(frames) or 1
        per = {NAMES.get(int(k), int(k)): round(float((d[:, 6] == k).sum()) / nf, 2) for k in np.unique(d[:, 6])}
        hi = (d[:, 5] >= 0.5).sum() / nf; mid = ((d[:, 5] >= 0.3) & (d[:, 5] < 0.5)).sum() / nf; lo = (d[:, 5] < 0.3).sum() / nf
        print(f'{tag:14s} frames {nf:4d} boxes/frame {len(d) / nf:5.2f}  conf>=.5 {hi:5.2f}  .3-.5 {mid:5.2f}  <.3 {lo:5.2f}  per class {per}')


def match(da, db, iou):
    """Mask over rows of da: has a box in db (same frame) with IoU >= iou."""
    m = np.zeros(len(da), bool)
    for n in np.unique(da[:, 0]):
        ia = np.where(da[:, 0] == n)[0]; ib = np.where(db[:, 0] == n)[0]
        if len(ib): m[ia] = iou_matrix(da[ia, 1:5], db[ib, 1:5]).max(1) >= iou
    return m


def cmd_consensus(a):
    da, db = load(a.run, a.a), load(a.run, a.b)
    ma, mb = match(da, db, a.iou), match(db, da, a.iou)
    out = da[ma]
    if a.mode == 'union':
        rows = []
        for n in np.unique(np.concatenate([da[:, 0], db[:, 0]])):
            both = np.concatenate([out[out[:, 0] == n], db[mb & (db[:, 0] == n)]])
            if len(both): rows.append(nms(both))
        out = np.concatenate(rows) if rows else np.zeros((0, 7))
    out = out[np.lexsort((out[:, 1], out[:, 0]))]
    nf = max(len(np.unique(da[:, 0])), 1)
    print(f'{a.a}: {len(da) / nf:.2f}/frame, {a.b}: {len(db) / nf:.2f}/frame, agree {ma.sum() / nf:.2f}/frame '
          f'({100 * ma.mean():.0f}% of {a.a}, {100 * mb.mean():.0f}% of {a.b}) -> {a.out}: {len(out) / nf:.2f}/frame')
    np.save(cache(a.run, a.out), out)
    mj = json.load(open(meta(a.run, a.a)))
    mj.update({'model': f'consensus({a.a},{a.b},iou={a.iou},{a.mode})'})
    json.dump(mj, open(meta(a.run, a.out), 'w'))


def cmd_diff(a):
    da, db = load(a.run, a.a), load(a.run, a.b)
    frames = [int(x) for x in a.frames.split(',')] if a.frames else np.unique(da[:, 0]).astype(int)
    ma = match(da, db, a.iou)
    for n in frames:
        rows = da[(da[:, 0] == n) & ~ma]
        print(f'f{n}: {len(rows)} boxes of {a.a} without a match in {a.b}:',
              ', '.join(f'{NAMES.get(int(k), int(k))}@{int(x1)},{int(y1)} {int(x2 - x1)}x{int(y2 - y1)} {c:.2f}' for _, x1, y1, x2, y2, c, k in rows))


def score_frame(rows, objs, ignore):
    """(detected object indices, false rows, dup rows) for one frame."""
    found, false, dup = set(), [], []
    g = np.array([[cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2] for cx, cy, w, h, _ in objs]).reshape(-1, 4)
    for r in rows:
        cx, cy = (r[1] + r[3]) / 2, (r[2] + r[4]) / 2
        if any(x1 <= cx <= x2 and y1 <= cy <= y2 for x1, y1, x2, y2 in ignore): continue
        hit = None
        if len(g):
            iou = iou_matrix(r[None, 1:5], g)[0]
            gc = np.column_stack([(g[:, 0] + g[:, 2]) / 2, (g[:, 1] + g[:, 3]) / 2])
            inside = (r[1] <= gc[:, 0]) & (gc[:, 0] <= r[3]) & (r[2] <= gc[:, 1]) & (gc[:, 1] <= r[4])
            cand = np.where((iou >= 0.1) | inside)[0]
            if len(cand): hit = int(cand[np.argmax(iou[cand])])
        if hit is None: false.append(r)
        elif hit in found: dup.append(r)
        else: found.add(hit)
    return found, false, dup


def cmd_score(a):
    inv = json.load(open(a.inventory))['frames']
    print(f'{"tag":14s} {"conf":>4s} | ' + ' '.join(f'{"f" + f:>12s}' for f in inv) + ' |  objects found  false   dup  recall')
    for tag in a.tags:
        d = load(a.run, tag)
        for conf in a.conf:
            cells, tot = [], [0, 0, 0, 0]
            for f, spec in inv.items():
                rows = d[(d[:, 0] == int(f)) & (d[:, 5] >= conf)]
                found, false, dup = score_frame(rows, spec['objects'], spec['ignore'])
                n = len(spec['objects']); cells.append(f'{len(found)}/{n} f{len(false)} d{len(dup)}')
                tot[0] += n; tot[1] += len(found); tot[2] += len(false); tot[3] += len(dup)
            print(f'{tag:14s} {conf:4.2f} | ' + ' '.join(f'{c:>12s}' for c in cells) + f' |  {tot[0]:5d} {tot[1]:5d} {tot[2]:6d} {tot[3]:5d}  {tot[1] / max(tot[0], 1):.2f}')
            if a.verbose:
                for f, spec in inv.items():
                    rows = d[(d[:, 0] == int(f)) & (d[:, 5] >= conf)]
                    found, false, dup = score_frame(rows, spec['objects'], spec['ignore'])
                    miss = [o for i, o in enumerate(spec['objects']) if i not in found]
                    print(f'    f{f} missed: ' + ', '.join(f'{k}@{cx},{cy}' for cx, cy, w, h, k in miss) + ' | false: ' +
                          ', '.join(f'{NAMES.get(int(r[6]), int(r[6]))}@{int(r[1])},{int(r[2])} {int(r[3]-r[1])}x{int(r[4]-r[2])} {r[5]:.2f}' for r in false))


if __name__ == '__main__':
    p = argparse.ArgumentParser(); s = p.add_subparsers(dest='cmd', required=True)
    d = s.add_parser('draw'); d.add_argument('run'); d.add_argument('tags', nargs='+'); d.add_argument('--video', required=True)
    d.add_argument('--frames', default='150,300,600,750,900,1050'); d.add_argument('--out', required=True)
    t = s.add_parser('stats'); t.add_argument('run'); t.add_argument('tags', nargs='+')
    c = s.add_parser('consensus'); c.add_argument('run'); c.add_argument('a'); c.add_argument('b')
    c.add_argument('--iou', type=float, default=0.3); c.add_argument('--out', required=True); c.add_argument('--mode', default='and', choices=['and', 'union'])
    f = s.add_parser('diff'); f.add_argument('run'); f.add_argument('a'); f.add_argument('b'); f.add_argument('--frames'); f.add_argument('--iou', type=float, default=0.3)
    sc = s.add_parser('score'); sc.add_argument('run'); sc.add_argument('tags', nargs='+'); sc.add_argument('--inventory', required=True)
    sc.add_argument('--conf', type=float, nargs='+', default=[0.15, 0.3]); sc.add_argument('-v', '--verbose', action='store_true')
    a = p.parse_args()
    {'draw': cmd_draw, 'score': cmd_score, 'stats': cmd_stats, 'consensus': cmd_consensus, 'diff': cmd_diff}[a.cmd](a)
