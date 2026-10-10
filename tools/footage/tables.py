"""Markdown tables for docs/FOOTAGE_FINDINGS.md from the run directories (numbers only, no frames).

  python -I tables.py sources|label_free|audit|minband|ground      # RUNS=runs/footage by default
Run directories: runs/footage/<clip> as written by run_clip.sh (dev-amad-test1 for the dev convoy clip).
"""
import sys, os, json, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from audit import metrics

R = os.environ.get('RUNS', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'runs', 'footage'))
CLIPS = [  # (label, run dir, split)
    ('MEVA 16-00-14 (4K)', 'meva-2018-03-13.16-00-14-bf', 'tuning'),
    ('amad-test1 (RGB 596x336)', 'dev-amad-test1', 'dev'),
    ('hituav-60m-30_1 (thermal)', 'hituav-60m-30_1', 'dev'),
    ('amad-test2 (RGB 596x336)', 'amad-test2', 'held-out'),
    ('mvt-test10 (RGB 720p)', 'mvt-test10', 'held-out'),
    ('meva-uav-0307-1720 (RGB 1080p)', 'meva-uav-0307-1720', 'held-out'),
    ('hituav-120m-30_3 (thermal)', 'hituav-120m-30_3', 'held-out'),
    ('hituav-70m-90_1 (thermal)', 'hituav-70m-90_1', 'held-out'),
]
VARIANTS = [('old', 'old'), ('det-legacy', 'new detector, old tracker'), ('det-newtracker', 'new detector + tracker'), ('', 'improved (det + MTI)')]


def mb(d):
    p = os.path.join(d, 'minband.jsonl'); out = {}
    if os.path.exists(p):
        for l in open(p):
            x = json.loads(l); out[x['run']] = x['result']
    return out


def f(v, fmt='%.2f'):
    return '-' if v is None else fmt % v


def label_free():
    print('| Clip | Split | Pipeline | Tracks | Births/min | Median track (s) | Entities/frame | Dets in a >=1 s track | Motion-only/frame | Static: n, std (m), KF speed (m/s) | MinBand B/s at 0.15 / 0.5 m | Mean error (cm) at 0.15 / 0.5 m |')
    print('|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|')
    for lab, run, split in CLIPS:
        base = os.path.join(R, run)
        if not os.path.exists(os.path.join(base, 'summary.json')): continue
        for sub, name in VARIANTS:
            d = os.path.join(base, sub) if sub else base
            if not os.path.exists(os.path.join(d, 'summary.json')): continue
            try: m = metrics(d)
            except Exception as e: print(f'<!-- {d}: {e} -->'); continue
            t = m['tracks']; s = m.get('static', {}); fu = m.get('fused', {})
            r = mb(d)
            b15, b5 = r.get('theta0.15', {}), r.get('theta0.5', {})
            st = '-' if not s.get('static_tracks') else f"{s['static_tracks']}, {s['position_std_m_median']:.2f}, {s['kf_speed_mps_median']:.2f}"
            print(f"| {lab} | {split} | {name} | {t.get('count', 0)} | {f(t.get('births_per_min'), '%.0f')} | {f(t.get('length_s', {}).get('median'), '%.1f')} | "
                  f"{f(t.get('mean_entities_per_frame'), '%.1f')} | {f(fu.get('in_confirmed_track_ge_1s'))} | {f(fu.get('motion_only_per_frame'), '%.1f') if sub == '' else '-'} | {st} | "
                  f"{f(b15.get('bytesPerSec'), '%.0f')} / {f(b5.get('bytesPerSec'), '%.0f')} | {f(b15.get('errMean', 0) * 100 if b15 else None, '%.1f')} / {f(b5.get('errMean', 0) * 100 if b5 else None, '%.1f')} |")


def sources():
    print('| Clip | Split | Source | Detections/frame | Agreement with another source | In a confirmed track >= 1 s |')
    print('|---|---|---|---:|---:|---:|')
    for lab, run, split in CLIPS:
        d = os.path.join(R, run)
        if not os.path.exists(os.path.join(d, 'detlog.npy')): continue
        m = metrics(d)
        for name, v in m['per_source'].items():
            nm = {'det': 'VisDrone appearance', 'mti': 'MTI (motion)'}.get(name, name)
            print(f"| {lab} | {split} | {nm} | {v['per_frame']:.2f} | {f(v['agreement_rate'])} | {f(v['in_confirmed_track_ge_1s'])} |")


def audit():
    print('| Clip | Split | Frames | Objects | Old: P det | Old: R det | Improved: P det | Improved: R det | Old: P track | Old: R track | Improved: P track | Improved: R track |')
    print('|---|---|---:|---:|---|---|---|---|---|---|---|---|')
    w = lambda x: 'n/a' if x[0] is None else f'{x[0]:.2f} [{x[1]:.2f}-{x[2]:.2f}]'
    for lab, run, split in CLIPS:
        p = os.path.join(R, run, 'audit', 'score.json')
        if not os.path.exists(p): continue
        s = json.load(open(p))
        print(f"| {lab} | {split} | {s['frames']} | {s['objects']} | {w(s['old_det']['precision'])} | {w(s['old_det']['recall'])} | {w(s['new_det']['precision'])} | {w(s['new_det']['recall'])} | "
              f"{w(s['old_trk']['precision'])} | {w(s['old_trk']['recall'])} | {w(s['new_trk']['precision'])} | {w(s['new_trk']['recall'])} |")


def minband():
    print('| Clip | Entities/frame | x264 CRF 23 native (kbit/s) | x264 lowest row (kbit/s) | MinBand 0.15 m (B/s) | err (cm) | telemetry 450 B/s, 5 % loss: B/s, err (cm) | lora 1500 B/s, 10 %: B/s, err | hf 8000 B/s, 1 %: B/s, err | x264 native / MinBand | x264 lowest / MinBand |')
    print('|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|')
    for lab, run, split in CLIPS:
        if split != 'held-out': continue
        d = os.path.join(R, run); r = mb(d)
        if not r: continue
        h = {}; lo = None
        p = os.path.join(d, 'h264.csv')
        if os.path.exists(p):
            for row in csv.DictReader(open(p)):
                if row['crf'] == '23': h[row['resolution']] = int(row['bps'])
                if lo is None or int(row['bps']) < lo[0]: lo = (int(row['bps']), f"{row['resolution']} CRF {row['crf']}")
        nat = h.get('native'); b = r['theta0.15']['bytesPerSec'] * 8
        c = lambda k: f"{r[k]['bytesPerSec']:.0f}, {r[k]['errMean'] * 100:.0f}" if k in r else '-'
        print(f"| {lab} | {r['theta0.15']['entitiesMean']:.1f} | {f(nat / 1000 if nat else None, '%.0f')} | {lo[0] / 1000:.0f} ({lo[1]}) | {r['theta0.15']['bytesPerSec']:.0f} | {r['theta0.15']['errMean'] * 100:.1f} | "
              f"{c('telemetry')} | {c('lora')} | {c('hf')} | {f(nat / b if nat and b else None, '%.0fx')} | {lo[0] / b:.0f}x |")


def ground():
    print('| Clip | Split | Scale from | Boxes | Pitch (deg) | Height (m) | GSD centre (cm/px) | Bootstrap 90 % GSD (cm/px) | Walker median speed (m/s) |')
    print('|---|---|---|---:|---:|---:|---:|---|---:|')
    for lab, run, split in CLIPS:
        p = os.path.join(R, run, 'summary.json')
        if not os.path.exists(p): continue
        s = json.load(open(p)); g = s['ground']; fit = g.get('fit', {})
        ci = fit.get('gsd_centre_m_px_90ci_bootstrap')
        print(f"| {lab} | {split} | {fit.get('method', 'people')} | {g['fit_boxes']} | {g['pitch_deg']:.0f} | {g['height_m']:.0f} | {g['gsd_centre_cm']:.1f} | "
              f"{'-' if not ci else f'{ci[0] * 100:.1f}-{ci[1] * 100:.1f}'} | {f(s.get('walker_median_speed_mps'))} |")


{'label_free': label_free, 'sources': sources, 'audit': audit, 'minband': minband, 'ground': ground}[sys.argv[1]]()
