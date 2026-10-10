"""Pooled visual-audit precision / recall (Wilson 95 %) over groups of clips: python -I pooled.py (RUNS=runs/footage)."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit import wilson
R = os.environ.get('RUNS', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'runs', 'footage'))
G = {
    'dev (amad-test1, hituav-60m)': ['dev-amad-test1', 'hituav-60m-30_1'],
    'held-out RGB (amad-test2, mvt-test10, meva-0307)': ['amad-test2', 'mvt-test10', 'meva-uav-0307-1720'],
    'held-out thermal (hituav-120m, hituav-70m)': ['hituav-120m-30_3', 'hituav-70m-90_1'],
    'held-out, all five': ['amad-test2', 'mvt-test10', 'meva-uav-0307-1720', 'hituav-120m-30_3', 'hituav-70m-90_1'],
    'held-out military vehicles (amad-test2, mvt-test10)': ['amad-test2', 'mvt-test10'],
}
w = lambda x: 'n/a' if x[0] is None else f'{x[0]:.2f} [{x[1]:.2f}-{x[2]:.2f}]'
print('| Group | Objects | Old P det | Old R det | Improved P det | Improved R det | Old P track | Old R track | Improved P track | Improved R track |')
print('|---|---:|---|---|---|---|---|---|---|---|')
for g, clips in G.items():
    tot = {k: {'tp': 0, 'fp': 0, 'fn': 0} for k in ('old_det', 'new_det', 'old_trk', 'new_trk')}; obj = 0
    for c in clips:
        s = json.load(open(os.path.join(R, c, 'audit', 'score.json'))); obj += s['objects']
        for k in tot:
            for f in ('tp', 'fp', 'fn'): tot[k][f] += s[k][f]
    cell = lambda k, m: w(wilson(tot[k]['tp'], tot[k]['tp'] + tot[k][m]))
    print(f"| {g} | {obj} | {cell('old_det', 'fp')} | {cell('old_det', 'fn')} | {cell('new_det', 'fp')} | {cell('new_det', 'fn')} | "
          f"{cell('old_trk', 'fp')} | {cell('old_trk', 'fn')} | {cell('new_trk', 'fp')} | {cell('new_trk', 'fn')} |")
