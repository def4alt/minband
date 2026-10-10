#!/usr/bin/env python3
"""Align a 3D reconstruction with MinBand's marker frame: writes the 4x4 `T` open3d_client.py takes.

MinBand positions are in the marker frame: origin at the centre of the printed marker on the floor,
+Y up, +X along the marker's width towards its right edge, -Z towards its top edge (the "MINBAND"
logo reads upright when the top edge is away from you). Metres. A reconstruction made with another
tool has its own frame (often OpenCV's: Y down, Z forward from the first camera), so pick three points
in it:

    1. the centre of the marker
    2. the middle of the marker's right edge (0.21 m from the centre along +X)
    3. a point on the floor beyond the marker's top edge (the forward, -Z, direction)

    python align.py room.ply --out T.txt                      # window: shift+click the 3 points in order, then press Q
    python align.py --points "x,y,z; x,y,z; x,y,z" --out T.txt  # the same 3 points typed in
    python align.py room.ply --out T.txt --scale-from-marker  # also fix the scale: point 2 is 0.21 m from point 1

`T` maps marker coordinates into the reconstruction's: scene = T @ [x, y, z, 1]. Its columns are the
marker's +X, +Y, +Z axes and origin in scene coordinates. Without --scale-from-marker the
reconstruction is taken to be in metres already (RGB-D and ARCore scans are).

Without a marker (ORIGIN HERE on the phone): the origin is where the phone stood, dropped onto the
floor; +X the phone's right, -Z where the camera looked. Pick the same three kinds of points there.
"""
import argparse
import sys

import numpy as np

MARKER_HALF_WIDTH_M = 0.21  # the A3 marker is 0.42 m wide


def frame_from_points(centre, right, forward, scale_from_marker=False):
    """4x4 marker -> scene from the three picked points (see module docstring)."""
    c, r, f = (np.asarray(p, float) for p in (centre, right, forward))
    x = r - c
    s = np.linalg.norm(x)
    if s < 1e-6:
        raise ValueError('points 1 and 2 coincide')
    x /= s
    fwd = f - c
    fwd -= fwd.dot(x) * x
    if np.linalg.norm(fwd) < 1e-6:
        raise ValueError('point 3 lies on the +X line through points 1 and 2')
    fwd /= np.linalg.norm(fwd)
    z = -fwd
    y = np.cross(z, x)  # right-handed: x cross y = z
    scale = s / MARKER_HALF_WIDTH_M if scale_from_marker else 1.0
    T = np.eye(4)
    T[:3, 0], T[:3, 1], T[:3, 2] = x * scale, y * scale, z * scale
    T[:3, 3] = c
    return T, scale


def pick_points(path):
    import open3d as o3d
    ext = path.lower().rsplit('.', 1)[-1]
    geom = None
    if ext in ('ply', 'obj', 'glb', 'gltf', 'stl', 'off'):
        m = o3d.io.read_triangle_mesh(path)
        if len(m.vertices) > 0:
            geom = o3d.geometry.PointCloud(m.vertices)
            if m.has_vertex_colors():
                geom.colors = m.vertex_colors
    if geom is None:
        geom = o3d.io.read_point_cloud(path)
    if len(geom.points) == 0:
        raise SystemExit(f'{path}: no points')
    print('Shift+click: 1 marker centre, 2 middle of its right edge, 3 floor beyond its top edge. Then press Q.', flush=True)
    vis = o3d.visualization.VisualizerWithEditing()
    vis.create_window('align: pick 3 points', width=1280, height=800)
    vis.add_geometry(geom)
    vis.run()
    vis.destroy_window()
    idx = vis.get_picked_points()
    if len(idx) < 3:
        raise SystemExit(f'picked {len(idx)} points, need 3')
    P = np.asarray(geom.points)
    return [P[i] for i in idx[-3:]]


def parse_points(text):
    pts = [np.asarray([float(v) for v in p.split(',')], float) for p in text.split(';') if p.strip()]
    if len(pts) != 3 or any(p.shape != (3,) for p in pts):
        raise SystemExit('--points needs "x,y,z; x,y,z; x,y,z"')
    return pts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('scene', nargs='?', help='reconstruction to pick the points in (.ply .pcd .obj ...)')
    ap.add_argument('--points', help='the 3 points as "x,y,z; x,y,z; x,y,z" instead of picking')
    ap.add_argument('--out', default='T.txt', help='where to write the 4x4 (default T.txt)')
    ap.add_argument('--scale-from-marker', action='store_true', help='point 2 is 0.21 m from point 1: fix the scale too')
    args = ap.parse_args(argv)
    if args.points:
        pts = parse_points(args.points)
    elif args.scene:
        pts = pick_points(args.scene)
    else:
        ap.error('give a scene file to pick in, or --points')
    T, scale = frame_from_points(*pts, scale_from_marker=args.scale_from_marker)
    np.savetxt(args.out, T, fmt='%.6f')
    np.set_printoptions(precision=4, suppress=True)
    print(T)
    print(f'scale {scale:.4f} scene units per metre; wrote {args.out}')
    print('check: open3d_client.py --transform', args.out, '--scene <your file>: the axes sit on the marker, red along its width, blue towards you.')


if __name__ == '__main__':
    sys.exit(main())
