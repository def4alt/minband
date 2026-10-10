"""MinBand geometry: camera <-> world-frame projection and ray/ground-plane intersection.

ONE world frame, everywhere -- non-negotiable (android/app/src/main/java/dev/minband/android/Origin.kt:3-18;
``server/src/geo.ts`` is a separate server-side-only lat/lon derivation of this frame and is NOT it):

    World: right-handed, metres, +Y up along gravity, origin and yaw arbitrary per session.

This is NOT ENU and NOT lat/lon. Nothing in this module (or ``pose_source.py``) converts GPS
coordinates into a local frame -- that would silently add a second world frame. If you find
yourself reaching for an ENU conversion here, stop: that is exactly the bug a prior decision in
this project ruled out.

Camera frame (OpenCV/pinhole convention -- the one ``tools/footage/track.py``'s ``Ground`` model
assumes, where image row ``v`` increases downward and pitch tilts the view down):

    +X right, +Y down (image row), +Z forward along the optical axis.

``R`` below is always camera-to-world: ``world_vec = R @ camera_vec``. Quaternions are w-LAST,
``(x, y, z, w)``, matching the wire's ``Message::Pose.quat`` (``core/src/wire.rs:47``; the identity
fixture at ``wire.rs:268`` is ``[0, 0, 0, 1]``).

World handedness note (judgment call, flagged for review): with camera +Z forward and world +Y up,
a right-handed world has +Z point *away* from the view direction (the usual "camera looks down -Z"
convention), not along it. ``pitch_height_pose`` below encodes that choice; see
``shared/test_geom.py`` for the numeric proof that it reproduces ``Ground`` exactly.

``Ground`` (``tools/footage/track.py:264-283``: fixed pitch/height, no yaw, camera forced above the
world's vertical axis) is not imported here -- ``track.py`` pulls in ``scipy.optimize`` at module
level, which is not installed in this venv, so importing it would crash this module for no reason.
Instead, ``to_ground``/``backproject_ray`` below are the general form, and ``pitch_height_pose``
builds the (R, p) pair for which they collapse back to exactly ``Ground``'s formulas (cross-checked
in the self-test, not just asserted).
"""
import math

import numpy as np


def intrinsics(width, height, hfov_deg):
    """Pinhole K for a given horizontal FOV, principal point at the image centre. Matches
    ``tools/footage/track.py:656`` (``f = (W/2) / tan(hfov/2)``); same ``--hfov`` default of 85 deg."""
    f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    return np.array([[f, 0.0, width / 2.0], [0.0, f, height / 2.0], [0.0, 0.0, 1.0]])


def quat_to_matrix(q):
    """w-last quaternion ``(x, y, z, w)`` -> camera-to-world rotation matrix (3x3)."""
    x, y, z, w = (float(c) for c in q)
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n < 1e-12:
        return np.eye(3)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def matrix_to_quat(R):
    """Camera-to-world rotation matrix -> w-last quaternion ``(x, y, z, w)``. Inverse of
    ``quat_to_matrix`` (standard Shepperd's-method-free trace formula; fine for the well-conditioned
    rotations this project produces)."""
    R = np.asarray(R, dtype=np.float64)
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        w = 0.25 * s
        x = (R[2, 1] - R[1, 2]) / s
        y = (R[0, 2] - R[2, 0]) / s
        z = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    return np.array([x, y, z, w])


def slerp(q0, q1, t):
    """Spherical linear interpolation between two w-last quaternions at ``t`` in [0, 1]. Used by
    ``pose_source.py`` to interpolate orientation at a frame timestamp that falls between two pose
    samples."""
    q0 = np.asarray(q0, dtype=np.float64)
    q1 = np.asarray(q1, dtype=np.float64)
    q0 = q0 / np.linalg.norm(q0)
    q1 = q1 / np.linalg.norm(q1)
    dot = float(np.dot(q0, q1))
    if dot < 0.0:  # take the short way round
        q1, dot = -q1, -dot
    if dot > 0.9995:  # nearly parallel: linear interp is numerically safer than slerp's 0/0
        out = q0 + t * (q1 - q0)
        return out / np.linalg.norm(out)
    theta0 = math.acos(max(-1.0, min(1.0, dot)))
    theta = theta0 * t
    q2 = q1 - q0 * dot
    q2 = q2 / np.linalg.norm(q2)
    return q0 * math.cos(theta) + q2 * math.sin(theta)


def pitch_height_pose(pitch, height):
    """Camera-to-world ``(R, p)`` for a static camera ``height`` metres above world Y=0, tilted down
    by ``pitch`` from horizontal, yaw=0, no roll -- the pose ``tools/footage/track.py``'s ``Ground``
    implicitly assumes. Feeding this into ``to_ground``/``backproject_ray`` reproduces
    ``Ground.to_ground`` exactly; see ``shared/test_geom.py``."""
    cp, sp = math.cos(pitch), math.sin(pitch)
    R = np.array([
        [1.0, 0.0, 0.0],
        [0.0, -cp, -sp],
        [0.0, sp, -cp],
    ])
    p = np.array([0.0, float(height), 0.0])
    return R, p


def euler_to_matrix(yaw, pitch, roll=0.0):
    """Camera-to-world rotation from yaw (about world +Y), pitch (down from horizontal) and roll
    (about the camera's own forward axis), all radians. ``euler_to_matrix(0, pitch, 0)`` is exactly
    ``pitch_height_pose(pitch, h)[0]``; yaw/roll generalise it for a gimbal that isn't yaw=0/roll=0
    (used by the DJI .SRT adapter in ``pose_source.py`` when gimbal angles are present)."""
    cy_, sy_ = math.cos(yaw), math.sin(yaw)
    ry = np.array([[cy_, 0.0, sy_], [0.0, 1.0, 0.0], [-sy_, 0.0, cy_]])
    r_tilt, _ = pitch_height_pose(pitch, 0.0)
    cr, sr = math.cos(roll), math.sin(roll)
    r_roll = np.array([[cr, -sr, 0.0], [sr, cr, 0.0], [0.0, 0.0, 1.0]])
    return ry @ r_tilt @ r_roll


def backproject_ray(K, R, u, v):
    """Pixel ``(u, v)`` -> ray direction in world frame (not normalised -- callers only need its
    direction, e.g. ``ray_ground_intersection``)."""
    d_cam = np.linalg.inv(K) @ np.array([u, v, 1.0])
    return R @ d_cam


def ray_ground_intersection(p, d_world, ground_y=0.0):
    """Intersect a camera ray (origin ``p``, direction ``d_world``) with the world ground plane
    ``Y=ground_y``. Returns ``(X, Z)`` world metres, or ``None`` if the ray can't hit the plane
    (looking level or up, from above the plane)."""
    p = np.asarray(p, dtype=np.float64)
    dy = d_world[1]
    if p[1] > ground_y and dy >= -1e-9:
        return None
    if p[1] < ground_y and dy <= 1e-9:
        return None
    t = (ground_y - p[1]) / dy
    if t <= 0:
        return None
    hit = p + t * d_world
    return float(hit[0]), float(hit[2])


def to_ground(K, R, p, u, v, ground_y=0.0):
    """Pixel ``(u, v)`` -> ``(X, Z)`` world metres on the ground plane, for an arbitrary pose
    ``(K, R, p)``. Generalises ``Ground.to_ground`` (fixed pitch/height, camera forced above the
    world's vertical axis) to any camera position and orientation."""
    return ray_ground_intersection(p, backproject_ray(K, R, u, v), ground_y)


def project(K, R, p, world_pt):
    """World point ``(X, Y, Z)`` -> pixel ``(u, v)``, or ``None`` if it's behind the camera."""
    R = np.asarray(R, dtype=np.float64)
    p = np.asarray(p, dtype=np.float64)
    cam = R.T @ (np.asarray(world_pt, dtype=np.float64) - p)
    if cam[2] <= 1e-9:
        return None
    px = K @ cam
    return float(px[0] / px[2]), float(px[1] / px[2])


def _ground_reference(f, cx, cy, pitch, h, u, v):
    """``tools/footage/track.py:264-283``'s ``Ground.range_t``/``to_ground`` formulas, copied in
    (not imported: ``track.py`` pulls in ``scipy.optimize`` at module scope, which this venv does
    not have installed). Used only by the self-check below to prove ``to_ground`` + the general
    ``K, R, p`` path reproduce it, not to re-derive geometry the production code relies on."""
    yc = (v - cy) / f
    den = math.sin(pitch) + yc * math.cos(pitch)
    t = h / den if den > 1e-3 else float('nan')
    xc = (u - cx) / f
    return xc * t, t * (math.cos(pitch) - yc * math.sin(pitch))


def _demo():
    width, height, hfov = 1280, 720, 85.0
    K = intrinsics(width, height, hfov)
    f, cx, cy = K[0, 0], K[0, 2], K[1, 2]

    for pitch_deg, h in [(69.0, 25.0), (45.0, 8.0), (30.0, 5.0)]:
        pitch = math.radians(pitch_deg)
        R, p = pitch_height_pose(pitch, h)

        # R is a proper rotation: orthonormal, right-handed.
        assert np.allclose(R @ R.T, np.eye(3), atol=1e-9), 'R not orthonormal'
        assert abs(np.linalg.det(R) - 1.0) < 1e-9, 'R not a proper (det=+1) rotation'

        for u, v in [(cx, cy), (cx + 200, cy + 150), (cx - 300, cy - 40)]:
            want = _ground_reference(f, cx, cy, pitch, h, u, v)
            got = to_ground(K, R, p, u, v)
            assert got is not None, f'pitch={pitch_deg} u={u} v={v}: ray missed the ground plane'
            got_x, got_z = got
            # Ground.to_ground's "forward" axis is this module's -Z (see module docstring).
            assert math.isclose(got_x, want[0], abs_tol=1e-6), (got_x, want[0])
            assert math.isclose(-got_z, want[1], abs_tol=1e-6), (-got_z, want[1])

            # Round trip: back-project the ground hit, must land on the same pixel.
            back = project(K, R, p, (got_x, 0.0, got_z))
            assert back is not None
            assert math.isclose(back[0], u, abs_tol=1e-6), back
            assert math.isclose(back[1], v, abs_tol=1e-6), back

    # Quaternion <-> matrix round trip, w-last convention (identity is [0, 0, 0, 1]).
    assert np.allclose(quat_to_matrix([0.0, 0.0, 0.0, 1.0]), np.eye(3), atol=1e-9)
    for pitch_deg in (10.0, 40.0, 80.0):
        R, _ = pitch_height_pose(math.radians(pitch_deg), 10.0)
        q = matrix_to_quat(R)
        assert np.allclose(quat_to_matrix(q), R, atol=1e-7), pitch_deg

    # Slerp: t=0 and t=1 return the endpoints; halfway is a unit quaternion between them.
    q0 = np.array([0.0, 0.0, 0.0, 1.0])
    q1 = matrix_to_quat(pitch_height_pose(math.radians(60.0), 10.0)[0])
    assert np.allclose(slerp(q0, q1, 0.0), q0, atol=1e-9)
    assert np.allclose(slerp(q0, q1, 1.0), q1, atol=1e-9)
    qh = slerp(q0, q1, 0.5)
    assert math.isclose(float(np.linalg.norm(qh)), 1.0, abs_tol=1e-9)

    # euler_to_matrix(0, pitch, 0) must equal pitch_height_pose(pitch, *)[0].
    for pitch_deg in (15.0, 55.0):
        pitch = math.radians(pitch_deg)
        assert np.allclose(euler_to_matrix(0.0, pitch, 0.0), pitch_height_pose(pitch, 1.0)[0], atol=1e-9)

    print('shared/geom.py self-check: OK')


if __name__ == '__main__':
    _demo()
