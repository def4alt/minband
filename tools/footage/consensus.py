#!/usr/bin/env python3
"""Consensus of two detectors: keep a detection of cache A only where an independently trained
model (cache B) also fired on the same frame at the same place (IoU >= --iou, any class), or where
A alone is very sure (conf >= --solo). Writes detections-<out>.npy and detect-<out>.json so
track.py can take it as a source.

  python consensus.py DIR --a det --b coco --out cons [--iou 0.3] [--solo 0.85]

Cache rows: frame, x1, y1, x2, y2, conf, class (MinBand id). Boxes from B are matched by geometry
only: the two models disagree on classes for aerial views (a VisDrone model calls a car a car, a COCO
model from above often says truck or boat); the class kept is A's.
"""
import argparse, json, os, sys
import numpy as np


def iou_matrix(a, b):
    ax1, ay1, ax2, ay2 = a[:, 0:1], a[:, 1:2], a[:, 2:3], a[:, 3:4]
    bx1, by1, bx2, by2 = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    iw = np.clip(np.minimum(ax2, bx2) - np.maximum(ax1, bx1), 0, None)
    ih = np.clip(np.minimum(ay2, by2) - np.maximum(ay1, by1), 0, None)
    inter = iw * ih
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / np.clip(area_a + area_b - inter, 1e-6, None)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('dir'); p.add_argument('--a', default='det'); p.add_argument('--b', default='coco'); p.add_argument('--out', default='cons')
    p.add_argument('--iou', type=float, default=0.3); p.add_argument('--solo', type=float, default=0.85)
    p.add_argument('--b-conf', type=float, default=0.2, help='B boxes below this do not count as agreement')
    p.add_argument('--class-aware', action='store_true', help='agreement also needs the same coarse class (person vs vehicle)')
    a = p.parse_args()
    name = lambda t: 'detections.npy' if t == 'det' else f'detections-{t}.npy'
    jname = lambda t: 'detect.json' if t == 'det' else f'detect-{t}.json'
    A = np.load(os.path.join(a.dir, name(a.a))); B = np.load(os.path.join(a.dir, name(a.b)))
    B = B[B[:, 5] >= a.b_conf]
    keep = np.zeros(len(A), bool); agreed = np.zeros(len(A), bool)
    frames = np.unique(A[:, 0])
    for f in frames:
        ia = np.where(A[:, 0] == f)[0]; ib = np.where(B[:, 0] == f)[0]
        if len(ib):
            m = iou_matrix(A[ia, 1:5], B[ib, 1:5])
            if a.class_aware:
                same = (A[ia, 6:7] == 0) == (B[ib, 6] == 0)[None, :]
                m = np.where(same, m, 0.0)
            agreed[ia] = m.max(axis=1) >= a.iou
    keep = agreed | (A[:, 5] >= a.solo)
    out = A[keep]
    np.save(os.path.join(a.dir, name(a.out)), out)
    meta = json.load(open(os.path.join(a.dir, jname(a.a))))
    meta['consensus'] = {'a': a.a, 'b': a.b, 'iou': a.iou, 'solo': a.solo, 'b_conf': a.b_conf, 'kept': int(keep.sum()), 'of': int(len(A)), 'agreed': int(agreed.sum())}
    json.dump(meta, open(os.path.join(a.dir, jname(a.out)), 'w'))
    by_cls = {int(c): (int((A[:, 6] == c).sum()), int((out[:, 6] == c).sum())) for c in np.unique(A[:, 6])}
    print(f'kept {keep.sum()} of {len(A)} ({agreed.sum()} by agreement, {(keep & ~agreed).sum()} solo >= {a.solo}); per class (before, after): {by_cls}')
    print(f'B had {len(B)} boxes >= {a.b_conf} on {len(np.unique(B[:, 0]))} frames')


if __name__ == '__main__':
    main()
