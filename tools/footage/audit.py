"""Label-free evaluation of the footage detectors and tracker, plus a visual audit protocol.

There is no ground truth for the battlefield clips (or MEVA's UAV clips), so this measures what can
be measured without labels, identically on every clip:

  python audit.py metrics RUN [RUN ...]          # per-source and track statistics -> RUN/audit-metrics.json
  python audit.py sample  RUN VIDEO --out A      # fixed-seed frame sample, raw + annotated crops -> A/
  python audit.py score   A [A ...]              # precision / recall from A/labels.json, Wilson 95 % CI

metrics (from track.py's detlog.npy, summary.json and tracks.csv):
- per detector source: detections per detection frame; agreement rate with the other sources (an
  MTI mover absorbed by an appearance box, an appearance box confirmed by motion or by the other
  appearance model); the fraction of its detections that end up in a confirmed track lasting >= 1 s
  (a persistence proxy for precision: clutter rarely persists, objects do).
- tracks: count, length distribution, births per minute (a fragmentation proxy: one object that
  fragments is born many times), mean entities per frame, per class.

The visual audit is NOT ground truth. After the parameters were frozen, the developer looks at a
fixed-seed random sample of frames (default 12 per clip, seed 0) and records, per frame, the
objects they can make out (dismounts, vehicles), which drawn boxes are on one (TP), which are on
nothing or duplicate another (FP), and the objects without a box (FN). `sample` draws every
detection the improved pipeline passes to the tracker (appearance boxes in cyan/yellow, motion-only
movers in magenta; solid when the detection ended in a confirmed track, thin when not), with an id.
`score` then computes, for the old pipeline (appearance only) and the improved one (fusion), at the
detection level (all boxes) and at the track level (boxes in confirmed tracks), precision and
recall with Wilson intervals. One person's reading of small objects in compressed video: say so
wherever the numbers are used.

labels.json: {"auditor": "...", "frozen_at": "<commit>", "frames": {"<frame>": {"tp": [ids], "fp": [ids],
"dup": [ids], "fn": <objects without a box>, "objects": <objects visible>, "note": "..."}}}
"""
import argparse, json, math, os, sys
import numpy as np
import cv2

MOVER = 100
VEHICLES = {1, 2, 3, 5, 7, 101}


def wilson(x, n, z=1.96):
    if n == 0: return [None, None, None]
    p = x / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [p, max(0.0, c - h), min(1.0, c + h)]


def load_run(run):
    s = json.load(open(os.path.join(run, 'summary.json')))
    lp = os.path.join(run, 'detlog.npy')
    log = np.load(lp) if os.path.exists(lp) else None  # the original pipeline wrote none
    tr = np.loadtxt(os.path.join(run, 'tracks.csv'), delimiter=',', skiprows=1, ndmin=2)
    return s, log, tr


# ---- metrics -------------------------------------------------------------------------------------

def static_jitter(tr, min_s=5.0, max_disp=1.0, tick_hz=120):
    """Tracks that do not move (net displacement < `max_disp` m over > `min_s` s): their position std
    (per axis, RMS of x and z) and KF speed, and how much of the wander is global (the whole scene
    shifting together, i.e. registration) versus per object (detection box jitter)."""
    rows = {}
    for i in np.unique(tr[:, 1]):
        m = tr[tr[:, 1] == i]
        dur = (m[-1, 0] - m[0, 0]) / tick_hz
        if dur > min_s and math.hypot(m[-1, 3] - m[0, 3], m[-1, 5] - m[0, 5]) < max_disp: rows[int(i)] = m
    if not rows: return {'static_tracks': 0}
    stds = [math.sqrt((np.var(m[:, 3]) + np.var(m[:, 5])) / 2) for m in rows.values()]
    speeds = np.concatenate([np.hypot(m[:, 6], m[:, 8]) for m in rows.values()])
    # Deviation of each static track from its own mean, per tick; the per-tick median over tracks is the global part.
    dev = {}
    for i, m in rows.items():
        mx, mz = m[:, 3].mean(), m[:, 5].mean()
        for r in m: dev.setdefault(int(r[0]), []).append((r[3] - mx, r[5] - mz))
    glob, local = [], []
    for k, v in dev.items():
        if len(v) < 3: continue
        v = np.array(v); g = np.median(v, axis=0)
        glob.append(np.hypot(*g)); local.extend(np.hypot(*(v - g).T))
    return {'static_tracks': len(rows), 'position_std_m_median': float(np.median(stds)),
            'position_std_m_range': [float(np.min(stds)), float(np.max(stds))],
            'kf_speed_mps_median': float(np.median(speeds)),
            'global_rms_m': float(np.sqrt(np.mean(np.square(glob)))) if glob else None,
            'per_object_rms_m': float(np.sqrt(np.mean(np.square(local)))) if local else None}


def metrics(run, det_frames=None):
    s, log, tr = load_run(run)
    fps_ticks = 120
    out = {'run': run, 'sources': s.get('sources', ['det (original pipeline)']), 'per_source': {}}
    if log is not None:
        frames = det_frames or len(np.unique(log[:, 0])) or 1
        out['detection_frames'] = int(frames)
    for i, name in enumerate(s['sources'] if log is not None else []):
        m = log[log[:, 1] == i]
        if name == 'mti':
            agree = float(m[:, 10].mean()) if len(m) else None  # absorbed by an appearance box
        else:
            agree = float((m[:, 11] >= 2).mean()) if len(m) else None  # same object from another source (motion or model)
        conf1s = float((m[:, 9] >= 1.0).mean()) if len(m) else None
        out['per_source'][name] = {'detections': int(len(m)), 'per_frame': len(m) / frames, 'agreement_rate': agree,
                                   'in_confirmed_track_ge_1s': conf1s}
    if log is not None:  # fused detections (what the tracker saw): not absorbed
        fused = log[log[:, 10] == 0]
        out['fused'] = {'per_frame': len(fused) / frames, 'in_confirmed_track_ge_1s': float((fused[:, 9] >= 1.0).mean()) if len(fused) else None,
                        'motion_only_per_frame': float((fused[:, 2] == MOVER).sum()) / frames}
    if len(tr):
        ids, counts = np.unique(tr[:, 1], return_counts=True)
        nframes = len(np.unique(tr[:, 0]))
        span_s = (tr[:, 0].max() - tr[:, 0].min()) / fps_ticks
        fps = len(np.unique(tr[:, 0])) / max(span_s, 1e-9)
        lens = counts / fps
        cls = {int(i): int(np.bincount(tr[tr[:, 1] == i, 2].astype(int)).argmax()) for i in ids}
        dur = s['duration_s']
        out['tracks'] = {
            'count': int(len(ids)), 'length_s': {'p10': float(np.quantile(lens, 0.1)), 'median': float(np.median(lens)),
                                                 'p90': float(np.quantile(lens, 0.9)), 'mean': float(lens.mean()), 'max': float(lens.max())},
            'ge_5s': int((lens >= 5).sum()), 'births_per_min': len(ids) / dur * 60, 'mean_entities_per_frame': len(tr) / max(nframes, 1),
            'by_class': {'dismount': sum(1 for c in cls.values() if c == 0), 'vehicle': sum(1 for c in cls.values() if c in VEHICLES and c != 101),
                         'armoured': sum(1 for c in cls.values() if c == 101), 'mover': sum(1 for c in cls.values() if c == MOVER)},
        }
        out['static'] = static_jitter(tr)
    else:
        out['tracks'] = {'count': 0}
    out['ground'] = s['ground']
    return out


def cmd_metrics(a):
    for run in a.runs:
        m = metrics(run)
        json.dump(m, open(os.path.join(run, 'audit-metrics.json'), 'w'), indent=2)
        print(json.dumps(m, indent=2))


# ---- visual audit sample -------------------------------------------------------------------------

COL = {'app_person': (255, 255, 0), 'app_vehicle': (0, 220, 255), 'mti': (255, 0, 255), 'old_only': (0, 255, 0)}


def sample_frames(frames, n, seed):
    rng = np.random.default_rng(seed)
    return sorted(int(x) for x in rng.choice(np.array(sorted(frames)), size=min(n, len(frames)), replace=False))


def crops_for(w, h, target_w):
    """Tiles at full resolution, about target_w wide, so small objects stay legible."""
    nx = max(1, int(round(w / target_w))); ny = max(1, int(round(h / (target_w * 9 / 16))))
    xs = np.linspace(0, w, nx + 1).astype(int); ys = np.linspace(0, h, ny + 1).astype(int)
    return [(xs[i], ys[j], xs[i + 1], ys[j + 1]) for j in range(ny) for i in range(nx)]


def iou(a, b):
    iw = min(a[2], b[2]) - max(a[0], b[0]); ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0: return 0.0
    i = iw * ih
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i)


def frame_boxes(log, n):
    """What the tracker saw at frame n (not absorbed by fusion): (row, box, cls, confirmed in a track >= 1 s... any length)."""
    return [(i, r[4:8].tolist(), int(r[2]), bool(r[8] > 0 and r[9] > 0)) for i, r in enumerate(log) if int(r[0]) == n and r[10] == 0]


def union_boxes(new, old):
    """Pair the old pipeline's boxes with the new one's (IoU >= 0.3, both appearance or any), so each
    object gets one id; returns a list of items with membership in each run."""
    items = [{'box': b, 'cls': k, 'new': True, 'new_confirmed': c, 'old': False, 'old_confirmed': False} for _, b, k, c in new]
    for _, b, k, c in old:
        best, bi = 0.0, None
        for it in items:
            if it['old'] or not it['new']: continue
            v = iou(it['box'], b)
            if v > best: best, bi = v, it
        if bi is not None and best >= 0.3: bi['old'] = True; bi['old_confirmed'] = c
        else: items.append({'box': b, 'cls': k, 'new': False, 'new_confirmed': False, 'old': True, 'old_confirmed': c})
    return items


def cmd_sample(a):
    _, log, _ = load_run(a.run)
    olog = load_run(a.old)[1] if a.old else None
    os.makedirs(a.out, exist_ok=True)
    frames = set(np.unique(log[:, 0]).astype(int))
    if olog is not None: frames &= set(np.unique(olog[:, 0]).astype(int)) | set()
    frames = sample_frames(frames, a.n, a.seed)
    cap = cv2.VideoCapture(a.video)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    tiles = crops_for(W, H, a.tile_width)
    tw = max(t[2] - t[0] for t in tiles)
    up = max(1.0, a.min_scale_px / tw)  # small videos: upscale for legibility
    index = {'run': a.run, 'old': a.old, 'video': os.path.basename(a.video), 'seed': a.seed, 'n': a.n, 'frames': {},
             'note': 'post-hoc visual audit by the developer after freezing; evaluation only, not ground truth'}
    for n in frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n); ok, fr = cap.read()
        if not ok: continue
        items = union_boxes(frame_boxes(log, n), frame_boxes(olog, n) if olog is not None else [])
        ann = cv2.resize(fr, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC) if up > 1 else fr.copy()
        raw = ann.copy()
        boxes = {}
        for bid, it in enumerate(items, 1):
            x1, y1, x2, y2 = (v * up for v in it['box']); k = it['cls']
            kind = 'old_only' if not it['new'] else ('mti' if k == MOVER else ('app_vehicle' if k in VEHICLES else 'app_person'))
            pad = 3
            cv2.rectangle(ann, (int(x1) - pad, int(y1) - pad), (int(x2) + pad, int(y2) + pad), COL[kind], 2 if (it['new_confirmed'] or it['old_confirmed']) else 1)
            cv2.putText(ann, str(bid), (int(x2) + pad + 1, int(y1) + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, COL[kind], 1, cv2.LINE_AA)
            boxes[bid] = {**it, 'box': [round(float(v), 1) for v in it['box']]}
        index['frames'][str(n)] = {'boxes': boxes, 'tiles': []}
        for ti, (x0, y0, x1, y1) in enumerate(tiles):
            sl = (slice(int(y0 * up), int(y1 * up)), slice(int(x0 * up), int(x1 * up)))
            base = os.path.join(a.out, f'f{n:06d}_t{ti}')
            cv2.imwrite(base + '_raw.jpg', raw[sl], [cv2.IMWRITE_JPEG_QUALITY, 92])
            cv2.imwrite(base + '_ann.jpg', ann[sl], [cv2.IMWRITE_JPEG_QUALITY, 92])
            index['frames'][str(n)]['tiles'].append(os.path.basename(base))
    json.dump(index, open(os.path.join(a.out, 'sample.json'), 'w'), indent=1)
    print(f'{len(frames)} frames x {len(tiles)} tiles (x{up:.1f}) -> {a.out}')


# ---- score ---------------------------------------------------------------------------------------

def score_dir(d):
    """Precision / recall of the old and the new pipeline from the developer's labels, at the detection
    level (every box the tracker saw) and the track level (boxes in confirmed tracks)."""
    idx = json.load(open(os.path.join(d, 'sample.json')))
    lab = json.load(open(os.path.join(d, 'labels.json')))
    res = {k: {'tp': 0, 'fp': 0, 'fn': 0} for k in ('old_det', 'new_det', 'old_trk', 'new_trk')}
    objects = 0; nfr = 0
    for n, fr in idx['frames'].items():
        L = lab['frames'].get(n)
        if L is None: continue
        nfr += 1
        boxes = fr['boxes']
        tp = set(map(int, L.get('tp', []))); fp = set(map(int, L.get('fp', []))) | set(map(int, L.get('dup', [])))
        missing = set(map(int, boxes)) - tp - fp
        if missing: sys.exit(f'{d} frame {n}: boxes not labelled: {sorted(missing)}')
        fn = int(L.get('fn', 0)); objects += len(tp) + fn
        for bid, b in boxes.items():
            is_tp = int(bid) in tp
            for run, flag, conf in (('old', b['old'], b['old_confirmed']), ('new', b['new'], b['new_confirmed'])):
                # detection level: a TP box only the other run has is an object this run missed
                if flag: res[run + '_det']['tp' if is_tp else 'fp'] += 1
                elif is_tp: res[run + '_det']['fn'] += 1
                # track level: only boxes in confirmed tracks count as reported
                if flag and conf: res[run + '_trk']['tp' if is_tp else 'fp'] += 1
                elif is_tp: res[run + '_trk']['fn'] += 1
        for k in res: res[k]['fn'] += fn
    out = {'dir': d, 'frames': nfr, 'objects': objects, 'auditor': lab.get('auditor'), 'frozen_at': lab.get('frozen_at'),
           'note': 'post-hoc visual audit by the developer after freezing; evaluation only, not ground truth'}
    for k, v in res.items():
        out[k] = {**v, 'precision': wilson(v['tp'], v['tp'] + v['fp']), 'recall': wilson(v['tp'], v['tp'] + v['fn'])}
    return out


def fmt(w):
    return 'n/a' if w[0] is None else f'{w[0]:.2f} [{w[1]:.2f}, {w[2]:.2f}]'


def cmd_score(a):
    for d in a.dirs:
        r = score_dir(d)
        print(f"{d}: {r['frames']} frames, {r['objects']} objects (developer's post-hoc visual audit, not ground truth)")
        for k in ('old_det', 'new_det', 'old_trk', 'new_trk'):
            v = r[k]
            print(f"  {k:8s} TP {v['tp']:4d} FP {v['fp']:4d} FN {v['fn']:4d}  P {fmt(v['precision'])}  R {fmt(v['recall'])}")
        json.dump(r, open(os.path.join(d, 'score.json'), 'w'), indent=2)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest='cmd', required=True)
    m = s.add_parser('metrics'); m.add_argument('runs', nargs='+')
    sm = s.add_parser('sample'); sm.add_argument('run'); sm.add_argument('video'); sm.add_argument('--out', required=True)
    sm.add_argument('--old', help="the old pipeline's run (track.py track --legacy-tracker output), drawn in the same frames")
    sm.add_argument('--n', type=int, default=12); sm.add_argument('--seed', type=int, default=0)
    sm.add_argument('--tile-width', type=int, default=1280, help='crop width in source pixels')
    sm.add_argument('--min-scale-px', type=int, default=1100, help='upscale crops narrower than this')
    sc = s.add_parser('score'); sc.add_argument('dirs', nargs='+')
    a = p.parse_args()
    {'metrics': cmd_metrics, 'sample': cmd_sample, 'score': cmd_score}[a.cmd](a)


if __name__ == '__main__':
    main()
