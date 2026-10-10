#!/usr/bin/env python3
"""Consensus of two detectors: keep a detection of cache A only where an independently trained
model (cache B) also fired on the same frame at the same place (IoU >= --iou, any class), or where
A alone is very sure (conf >= --solo). Writes detections-<out>.npy and detect-<out>.json so
track.py can take it as a source.

  python consensus.py DIR --a det --b coco --out cons [--iou 0.3] [--solo 0.85]
  python consensus.py DIR --a det --b coco --out cons3 --solo-vehicle 0.7 --solo-person 1.0 --keep 0.3

Cache rows: frame, x1, y1, x2, y2, conf, class (MinBand id). Boxes from B are matched by geometry
only: the two models disagree on classes for aerial views (a VisDrone model calls a car a car, a COCO
model from above often says truck or boat); the class kept is A's. --class-aware-person asks B for a
person where A says person (a COCO model from 25 m rarely calls a roof vent a person, so the
agreement means something; vehicles stay any-class).

Track-level consensus (--keep C): every A box at conf >= C is written, with an 8th column that says
whether the two models agreed on it (or A alone was sure). track.py --birth-agreed then lets a track
be born only from an agreed box but continue on any box: one model blinking no longer cuts a real
car's track, and a car that only one model sees at the frame edge keeps its id. Without --keep the
cache has the 7 columns of a detector cache, as before.
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
    p.add_argument('--solo-person', type=float, help='solo threshold for person boxes (default: --solo; 1.0 = agreement only)')
    p.add_argument('--solo-vehicle', type=float, help='solo threshold for vehicle boxes (default: --solo)')
    p.add_argument('--b-conf', type=float, default=0.2, help='B boxes below this do not count as agreement')
    p.add_argument('--class-aware', action='store_true', help='agreement also needs the same coarse class (person vs vehicle)')
    p.add_argument('--class-aware-person', action='store_true', help='agreement on a person box needs a B person box; vehicles any class')
    p.add_argument('--keep', type=float, help='track-level mode: also write A boxes at or above this conf, flagged as not agreed (8th column)')
    a = p.parse_args()
    name = lambda t: 'detections.npy' if t == 'det' else f'detections-{t}.npy'
    jname = lambda t: 'detect.json' if t == 'det' else f'detect-{t}.json'
    A = np.load(os.path.join(a.dir, name(a.a)))[:, :7]; B = np.load(os.path.join(a.dir, name(a.b)))[:, :7]
    B = B[B[:, 5] >= a.b_conf]
    agreed = np.zeros(len(A), bool)
    person = A[:, 6] == 0
    for f in np.unique(A[:, 0]):
        ia = np.where(A[:, 0] == f)[0]; ib = np.where(B[:, 0] == f)[0]
        if len(ib):
            m = iou_matrix(A[ia, 1:5], B[ib, 1:5])
            if a.class_aware:
                same = (A[ia, 6:7] == 0) == (B[ib, 6] == 0)[None, :]
                m = np.where(same, m, 0.0)
            elif a.class_aware_person:
                ok = (A[ia, 6:7] != 0) | (B[ib, 6] == 0)[None, :]
                m = np.where(ok, m, 0.0)
            agreed[ia] = m.max(axis=1) >= a.iou
    solo_p = a.solo if a.solo_person is None else a.solo_person
    solo_v = a.solo if a.solo_vehicle is None else a.solo_vehicle
    sure = agreed | np.where(person, A[:, 5] >= solo_p, A[:, 5] >= solo_v)
    keep = sure | (A[:, 5] >= a.keep) if a.keep is not None else sure
    out = np.column_stack([A[keep], sure[keep].astype(np.float64)]) if a.keep is not None else A[keep]
    np.save(os.path.join(a.dir, name(a.out)), out)
    meta = json.load(open(os.path.join(a.dir, jname(a.a))))
    meta['consensus'] = {'a': a.a, 'b': a.b, 'iou': a.iou, 'solo': a.solo, 'solo_person': solo_p, 'solo_vehicle': solo_v, 'b_conf': a.b_conf,
                         'class_aware': a.class_aware, 'class_aware_person': a.class_aware_person, 'keep': a.keep,
                         'kept': int(keep.sum()), 'of': int(len(A)), 'agreed': int(agreed.sum()), 'sure': int(sure.sum())}
    json.dump(meta, open(os.path.join(a.dir, jname(a.out)), 'w'))
    by_cls = {int(c): (int((A[:, 6] == c).sum()), int((out[:, 6] == c).sum()), int((out[:, 6] == c)[sure[keep]].sum())) for c in np.unique(A[:, 6])}
    print(f'kept {keep.sum()} of {len(A)} ({agreed.sum()} by agreement, {(sure & ~agreed).sum()} solo >= {solo_p}/{solo_v} person/vehicle'
          f'{f", {(keep & ~sure).sum()} unsure >= {a.keep} for continuation" if a.keep is not None else ""}); per class (before, after, sure): {by_cls}')
    print(f'B had {len(B)} boxes >= {a.b_conf} on {len(np.unique(B[:, 0]))} frames')


if __name__ == '__main__':
    main()
