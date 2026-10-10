"""Pose adapters for the MinBand footage pipeline.

Every adapter is a generator yielding ``(t, frame, K, R, p)``:

    t      seconds from clip start (float)
    frame  frame index (int, 0-based)
    K      3x3 intrinsics (shared.geom.intrinsics)
    R      camera-to-world rotation, 3x3 (shared.geom.quat_to_matrix / euler_to_matrix)
    p      camera position in world metres, shape (3,) -- or None if genuinely unknown

All in the single MinBand world frame (see ``shared/geom.py``'s module docstring: right-handed,
metres, +Y up, origin/yaw arbitrary per session). No adapter here converts GPS lat/lon into that
frame -- doing so would require picking an origin and axes (that's the app's Origin-lock, not a
footage-tool concern) and would quietly reintroduce ENU. Where a source only has GPS and no gimbal
orientation, position comes back as ``None`` rather than a guessed conversion.
"""
import bisect
import csv
import math
import re

try:
    from . import geom  # package import (e.g. `from shared import pose_source`)
except ImportError:
    import geom  # run directly: `python shared/pose_source.py` puts this dir on sys.path


def static_adapter(clip, width, height, n_frames, fps=30.0, hfov=85.0):
    """Fixed, seeded camera pose for a clip with no pose log at all -- the path this project
    actually exercises right now (``mvt-test10``, 1280x720; no real .SRT or CSV pose file exists
    for it or any other clip in the repo). Pitch/height are seeded from the measured table in
    ``docs/FOOTAGE_FINDINGS.md:409-423`` rather than re-derived or guessed; `clip` matches on
    substring so e.g. "mvt-test10" or a path containing it both work.

    `tools/footage/track.py:336`'s `fit_ground` can refine these from a clip's own detections; this
    adapter intentionally does not call it (optional, see `refine_with_track_py` below) -- the
    seeded baseline here is meant to stand on its own.
    """
    pitch_deg, height_m = _lookup_seed(clip)
    K = geom.intrinsics(width, height, hfov)
    R, p = geom.pitch_height_pose(math.radians(pitch_deg), height_m)
    for frame in range(n_frames):
        yield frame / fps, frame, K, R, p


# docs/FOOTAGE_FINDINGS.md:409-423 ("Ground scale" table): pitch (deg), height (m) per clip.
_SEED_TABLE = {
    'amad-test1': (51.0, 29.0),
    'hituav-60m-30_1': (45.0, 8.0),
    'amad-test2': (11.0, 9.0),
    'mvt-test10': (69.0, 25.0),
    'meva-uav-0307-1720': (80.0, 20.0),
    'hituav-120m-30_3': (66.0, 42.0),
    'hituav-70m-90_1': (45.0, 6.0),
}


def _lookup_seed(clip):
    for name, seed in _SEED_TABLE.items():
        if name in clip:
            return seed
    raise KeyError(f'no seeded pitch/height for clip {clip!r} in docs/FOOTAGE_FINDINGS.md\'s table '
                    f'-- add it there first rather than guessing one here')


def refine_with_track_py(clip, width, height, n_frames, dets, homs, args, fps=30.0, hfov=85.0):
    """Optional: refine the seeded static pose using the clip's own detections, via
    ``tools/footage/track.py:336``'s ``fit_ground`` (people/vehicle box widths). Needs `scipy`
    (``track.py`` imports ``scipy.optimize`` at module scope) -- not installed in this venv, so the
    import is deferred to call time rather than module load, and this function is simply unused
    until then. `dets`/`homs`/`args` are `fit_ground`'s own inputs (detections array, homography
    dict, argparse-like namespace with `person_width`/`vehicle_width`/`assume_pitch`)."""
    import os
    import sys
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from tools.footage.track import fit_ground  # deferred: pulls in scipy.optimize

    K = geom.intrinsics(width, height, hfov)
    cx, cy, f = K[0, 2], K[1, 2], K[0, 0]
    pitch, h, _info = fit_ground(dets, homs, f, cx, cy, args)
    R, p = geom.pitch_height_pose(pitch, h)
    for frame in range(n_frames):
        yield frame / fps, frame, K, R, p


def csv_adapter(path, fps=None):
    """Generic CSV pose log. Header row required, columns:

        frame (optional, else enumerated), t (optional, else frame/fps)
        either  fx, fy, cx, cy            (intrinsics directly)
        or      hfov, width, height       (intrinsics via shared.geom.intrinsics)
        qx, qy, qz, qw                    (w-last orientation quaternion)
        px, py, pz                        (optional, blank cells -> position unknown: None)

    Unknown/blank numeric cells elsewhere raise -- a CSV pose log claiming to have intrinsics or
    orientation but missing a field is a bad file, not a None case.
    """
    with open(path, newline='') as fh:
        rows = list(csv.DictReader(fh))
    for i, row in enumerate(rows):
        frame = int(row['frame']) if row.get('frame') not in (None, '') else i
        if row.get('t') not in (None, ''):
            t = float(row['t'])
        elif fps:
            t = frame / fps
        else:
            raise ValueError(f'{path}: row {i} has no t column and no fps given')

        if row.get('fx') not in (None, ''):
            K = [[float(row['fx']), 0.0, float(row['cx'])],
                 [0.0, float(row['fy']), float(row['cy'])],
                 [0.0, 0.0, 1.0]]
            import numpy as np
            K = np.array(K)
        else:
            K = geom.intrinsics(float(row['width']), float(row['height']), float(row['hfov']))

        q = [float(row['qx']), float(row['qy']), float(row['qz']), float(row['qw'])]
        R = geom.quat_to_matrix(q)

        if row.get('px') not in (None, ''):
            p = [float(row['px']), float(row['py']), float(row['pz'])]
            import numpy as np
            p = np.array(p)
        else:
            p = None

        yield t, frame, K, R, p


_SRT_TIME = re.compile(r'(\d+):(\d{2}):(\d{2})[.,](\d+)')
_SRT_FIELD = re.compile(r'\[\s*(gb_yaw|gb_pitch|gb_roll)\s*:\s*(-?[\d.]+)\s*\]', re.IGNORECASE)


def _srt_time_s(text):
    m = _SRT_TIME.search(text)
    if not m:
        return None
    h, mi, s, frac = m.groups()
    frac_s = int(frac) / (10 ** len(frac))
    return int(h) * 3600 + int(mi) * 60 + int(s) + frac_s


def srt_adapter(path, width, height, hfov=85.0, default_pitch_deg=0.0):
    """DJI .SRT subtitle track: one block per frame, a timestamp range and free-text metadata.
    NOTE: no .SRT file exists anywhere in this repo (no drone/phone pose log has been captured
    yet), so this adapter is unvalidated against real data -- it is the real-drone / future-phone
    path, built to the DJI format but not run against a real file. Treat it as needing a check the
    first time an actual .SRT shows up.

    Orientation: if the block has DJI gimbal fields (``[gb_yaw: ..] [gb_pitch: ..] [gb_roll: ..]``,
    present on newer DJI models only), R comes from those. Otherwise R falls back to a level camera
    pitched down by `default_pitch_deg` (crude; most DJI SRTs before ~2021 have no gimbal fields).

    Position: always ``None``. DJI SRTs carry GPS lat/lon/(rel/abs) altitude, a geodetic frame, not
    this project's world frame; turning GPS into a world-frame position needs a chosen origin and
    axes (the app's Origin-lock does this live, from ARCore/ARKit + the printed marker) -- doing it
    ad hoc here from absolute GPS would be exactly the ENU-style second frame this project forbids.
    """
    K = geom.intrinsics(width, height, hfov)
    with open(path, encoding='utf-8', errors='replace') as fh:
        content = fh.read()
    blocks = re.split(r'\r?\n\r?\n+', content.strip())
    t0 = None
    for frame, block in enumerate(blocks):
        t = _srt_time_s(block)
        if t is None:
            continue
        if t0 is None:
            t0 = t
        fields = dict(_SRT_FIELD.findall(block))
        if fields:
            yaw = math.radians(float(fields.get('gb_yaw', 0.0)))
            pitch = math.radians(float(fields.get('gb_pitch', 0.0)))
            roll = math.radians(float(fields.get('gb_roll', 0.0)))
            R = geom.euler_to_matrix(yaw, pitch, roll)
        else:
            R, _ = geom.pitch_height_pose(math.radians(default_pitch_deg), 0.0)
        yield t - t0, frame, K, R, None


def interpolate(samples, t):
    """Pose at arbitrary time `t`, interpolated between the bracketing `(t, frame, K, R, p)` pose
    samples (sorted by t -- the adapters' own output shape, so a list of their yields can be passed
    straight in). Returns `(K, R, p)`: rotation is slerped, position linearly interpolated (or None
    if either bracketing sample's is None). Clamps to the nearest endpoint outside the samples'
    time range."""
    ts = [s[0] for s in samples]
    if t <= ts[0]:
        _, _, K, R, p = samples[0]
        return K, R, p
    if t >= ts[-1]:
        _, _, K, R, p = samples[-1]
        return K, R, p
    i = bisect.bisect_right(ts, t) - 1
    t0, _, K0, R0, p0 = samples[i]
    t1, _, K1, R1, p1 = samples[i + 1]
    frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
    q = geom.slerp(geom.matrix_to_quat(R0), geom.matrix_to_quat(R1), frac)
    R = geom.quat_to_matrix(q)
    if p0 is None or p1 is None:
        p = None
    else:
        p = p0 + frac * (p1 - p0)
    return K0, R, p


def _demo():
    import math as _math
    import os
    import tempfile

    import numpy as np

    # static_adapter: seeded pitch/height, constant pose every frame, matches the FOOTAGE_FINDINGS table.
    samples = list(static_adapter('mvt-test10-clip.mp4', 1280, 720, n_frames=5, fps=10.0))
    assert len(samples) == 5
    assert [s[1] for s in samples] == [0, 1, 2, 3, 4]
    assert math.isclose(samples[3][0], 0.3)
    _, _, K, R, p = samples[0]
    assert K.shape == (3, 3) and R.shape == (3, 3) and p.shape == (3,)
    assert all(np.array_equal(s[4], p) for s in samples), 'static adapter must not move the camera'
    assert math.isclose(p[1], 25.0), 'seeded height for mvt-test10 is 25 m'
    try:
        static_adapter('no-such-clip', 100, 100, 1).__next__()
        raise AssertionError('unseeded clip should raise')
    except KeyError:
        pass

    # csv_adapter: direct intrinsics + quat, one row with a position and one without.
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'poses.csv')
        with open(path, 'w', newline='') as fh:
            fh.write('frame,t,fx,fy,cx,cy,qx,qy,qz,qw,px,py,pz\n')
            fh.write('0,0.0,900,900,640,360,0,0,0,1,1.0,2.0,3.0\n')
            fh.write('1,0.1,900,900,640,360,0,0,0,1,,,\n')
        rows = list(csv_adapter(path))
        assert len(rows) == 2
        assert np.allclose(rows[0][4], [1.0, 2.0, 3.0])
        assert rows[1][4] is None
        assert np.allclose(rows[0][3], np.eye(3))

    # srt_adapter: a two-block DJI-style file, one with gimbal fields, one without (fallback pitch).
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'clip.srt')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(
                '1\n00:00:00,000 --> 00:00:00,033\n'
                'SrtCnt : 1, DiffTime : 33ms\n'
                '[iso : 100] [gb_yaw: 10] [gb_pitch: -45] [gb_roll: 0]\n\n'
                '2\n00:00:00,033 --> 00:00:00,066\n'
                'SrtCnt : 2, DiffTime : 33ms\n'
                '[iso : 100] [latitude: 1.0] [longitude: 2.0]\n\n'
            )
        srows = list(srt_adapter(path, 1280, 720, default_pitch_deg=30.0))
        assert len(srows) == 2
        assert srows[0][4] is None and srows[1][4] is None, 'SRT must never guess a world position from GPS'
        assert math.isclose(srows[0][0], 0.0)
        assert math.isclose(srows[1][0], 0.033, abs_tol=1e-6)
        expect_gimbal = geom.euler_to_matrix(_math.radians(10), _math.radians(-45), 0.0)
        assert np.allclose(srows[0][3], expect_gimbal)
        expect_fallback, _ = geom.pitch_height_pose(_math.radians(30.0), 0.0)
        assert np.allclose(srows[1][3], expect_fallback)

    # interpolate: t=0 / t=1 endpoints, and a slerped/linear midpoint in between.
    R0, p0 = geom.pitch_height_pose(0.0, 10.0)
    R1, p1 = geom.pitch_height_pose(_math.radians(60.0), 20.0)
    samp = [(0.0, 0, K, R0, p0), (1.0, 1, K, R1, p1)]
    K_, R_, p_ = interpolate(samp, 0.0)
    assert np.allclose(R_, R0) and np.allclose(p_, p0)
    K_, R_, p_ = interpolate(samp, 1.0)
    assert np.allclose(R_, R1) and np.allclose(p_, p1)
    K_, R_, p_ = interpolate(samp, 0.5)
    assert np.allclose(p_, (p0 + p1) / 2)
    assert math.isclose(float(np.linalg.norm(geom.matrix_to_quat(R_))), 1.0, abs_tol=1e-9)
    # off-range clamps to the nearest endpoint
    K_, R_, p_ = interpolate(samp, 5.0)
    assert np.allclose(R_, R1) and np.allclose(p_, p1)

    print('shared/pose_source.py self-check: OK')


if __name__ == '__main__':
    _demo()
