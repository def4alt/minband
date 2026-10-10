# tools/movers: the movers feed for an external 3D renderer

A static 3D reconstruction of the scene (made with any tool) plus MinBand's live entities: only
what moves is animated, the reconstruction already holds the rest. The server classifies every
fused entity as moving or static and serves the movers at `GET /api/movers`; this directory has
the Open3D client and the frame alignment tool.

```
phone (edge)  --UDP-->  server :7777/:8080  --GET /api/movers (JSON, 20 Hz)-->  open3d_client.py
                                                                                 + your room.ply
```

## Run

```bash
pip install open3d numpy                     # Python 3.9-3.12 (3.13 works with open3d >= 0.20)
cd server && npm run start                   # the MinBand server (laptop)
python tools/movers/open3d_client.py --server http://<laptop-ip>:8080 --print          # text only, check the data
python tools/movers/open3d_client.py --server http://<laptop-ip>:8080 --scene room.ply --transform T.txt
```

Flags: `--scene` a point cloud or mesh (.ply .pcd .xyz .obj .glb .stl), shown as is; `--transform`
the 4x4 from `align.py` (marker frame -> your frame; without it the marker frame is the scene
frame); `--all` also shows static entities in grey; `--hz` poll rate (default 20).

The window shows a 1 m floor grid on the marker's floor plane, the marker axes (X red, Y green,
Z blue), one figure per mover: a person (cylinder + head, a small block for the nose on the side
it walks towards), a box per vehicle (car 1.8 x 1.5 x 4.5 m, bus, truck, armoured), a small box
for anything else. Spawns and despawns are printed. Between polls the client dead-reckons
`pos + vel * dt` (at most 1 s); after 2 s without a good poll the scene empties.

## `GET /api/movers?all=0|1`

```jsonc
{
  "t": 1791600000123,          // server ms the positions are valid for (snapshot + dead reckoning)
  "ageMs": 12,                 // dead reckoning applied to the last 30 Hz snapshot (<= 1000)
  "frame": { "origin": "marker centre (ORIGIN HERE: camera dropped onto the floor)", "units": "m",
             "up": "+Y", "forward": "-Z", "right": "+X",
             "heading": "deg, 0 along -Z, 90 along +X, clockwise from above" },
  "movers": [{
    "gid": "g3",               // stable id of the fused entity (across devices)
    "class": 0, "name": "dismount",   // core/src/classes.rs ids: 0 person, 1 bicycle, 2 car, 3 motorcycle,
                               //   5 bus, 7 truck, 56 chair, ..., 100 unclassified mover, 101 armoured
    "pos": [1.21, 0.0, -3.4],  // metres, marker frame; people at their feet, vehicles at the ground contact
    "vel": [0.9, 0.0, -0.4],   // m/s
    "speed": 0.98,
    "headingDeg": 66.0,        // direction of travel; held while slow; null until the first fast sample
    "moving": true,            // always true unless ?all=1
    "ce": 0.15,                // honest error radius, m (grows while the link is down)
    "coasting": false,         // the device missed its heartbeat: position is extrapolated
    "stale": false,
    "sources": [{ "deviceId": 34223, "id": 7 }]
  }]
}
```

Moving / static (`server/src/movers.ts`, hysteresis) judges a **robust position**: the median of
the entity's positions over the last second, refreshed every half second, so one lift flip (the
phone placing an occluded person a metre farther for one hit) does not move it. Static -> moving
when that position moved, from where it was 2 s ago, >= 0.5 m (a person; 0.7 m for carried
objects, 1 m for chairs, laptops and tvs) **across the ray** from the phone that sees it (the
bearing, which a monocular lift gets right whatever hid the feet), or **along the ray** by 40 % of
the range when that is more (a standing person's range wanders by +-0.4 m at 5 m between hits, so
2.4 m is needed at 6 m), while moving at least 0.2 m/s. A walker crossing the view is a mover even when their range is guesswork; one
walking straight at the phone with hidden legs is not seen until the range has moved that much.
Chairs, laptops, tvs and carried objects are judged across the ray only (their range steps when
someone passes in front of them).
Speed alone never makes a mover: a track born from two noisy hits (a seated person placed by the
phone's width prior) carries a velocity for a while though its position goes nowhere. Moving ->
static when for 5 s the robust position never left a 0.3 m ball around its current one (so a
walker who pauses keeps their figure). A new entity starts static and is not judged for its first
second (a track born from two hits that never gets a third would otherwise be a mover for the 2 s
it takes to die); a walking person flips 1-1.5 s after it appears. A new entity born within 1 m of
a mover, alive or gone less than 2 s ago, is that mover under a new id (the phone lost them for a
second, or put a second box on them) and starts as a mover, so the figure does not drop and start
over (same class only). The displacement judged is relative to the scene: when the entities being
observed moved together the way the phone did, the phone's pose is drifting (ARCore's ran 26 m in
9 s once with a walker filling the view) and its displacement is subtracted; otherwise the median
displacement of the observed chairs, laptops and carried objects, or of all observed entities when
there are three. The phone's own tag
(`OverlayMotion.kt`) is the same rule with the track's hit count (3) in place of the age and its
lift sigma in place of the range.
The same two fields, `moving` and `headingDeg`, are on every `GlobalEntity` of the 30 Hz
WebSocket snapshot.

## Frame and alignment (`align.py`)

MinBand positions are in the **marker frame**: origin at the centre of the printed marker on the
floor (`ios/Markers/minband-marker-a3.png`, 0.42 m wide), +Y up, +X along the marker's width
towards its right edge, -Z towards its top edge (the edge away from the "MinBand" text; the text
edge is +Z). Metres. Without a marker (ORIGIN HERE on the phone) the origin is where the phone
stood, dropped onto the floor, +X the phone's right, -Z where the camera looked.

Your reconstruction has its own frame (OpenCV's Y-down Z-forward from the first camera is common,
and photogrammetry is not metric). Put the marker on the floor **before** scanning, then pick three
points in the scan:

1. the centre of the marker,
2. the middle of the marker's right edge (0.21 m from the centre along +X),
3. a point on the floor beyond the marker's top edge (the forward, -Z, direction).

```bash
python tools/movers/align.py room.ply --out T.txt                 # shift+click the 3 points in order, press Q
python tools/movers/align.py --points "x,y,z; x,y,z; x,y,z" --out T.txt
python tools/movers/align.py room.ply --out T.txt --scale-from-marker   # not metric: point 2 fixes the scale
```

`T` maps marker coordinates into yours (`scene = T @ [x, y, z, 1]`); its columns are the marker's
+X, +Y, +Z and origin in your frame. Check: run the client with `--transform T.txt --scene room.ply`;
the axes must sit on the marker in the scan, red along its width, blue towards the text edge, and
a person walking in front of the phone must walk on the scanned floor. A 2 cm pick error on point 2
is a 5 degree yaw error (3 cm sideways at 3 m): pick on the edge, zoomed in.

## Your own Open3D loop

```python
from open3d_client import Feed, pose_matrix, make_body      # tools/movers on sys.path
feed = Feed('http://192.168.0.16:8080'); feed.start()
...
for row, pos in feed.current():                              # pos: dead-reckoned marker-frame position
    M = pose_matrix(pos, row['headingDeg'], T)               # 4x4 in your frame, facing the heading
    mesh.vertices = o3d.utility.Vector3dVector((M[:3, :3] @ base_vertices.T).T + M[:3, 3])
    vis.update_geometry(mesh)
```

`feed.current()` is thread-safe; `feed.status()` has the poll age and the last error.

## Checking the phone's tracking without the phone

Two scripts for when a mover does not show up (no MOVING tag on the phone, nothing in the feed):

```bash
# 1. The phone's own detector on a clip -> boxes CSV, replayed through the phone's tracking chain
#    (Lift3D -> Tracker -> OverlayMotion) by android/app/src/test/.../PhoneReplayTest.kt at 10 .. 1.25 Hz.
tools/footage/.venv/Scripts/python tools/movers/phone_detect.py tools/footage/clips/vtest.avi \
    --model android/app/src/main/assets/yolov8n-256.onnx \
    --out android/app/src/test/resources/replay/vtest-yolov8n-256.csv
cd android && ./gradlew :app:testDebugUnitTest --tests "dev.minband.android.PhoneReplayTest"

# 1b. The same replay on drone footage (a tools/footage run: detect, track, then this):
tools/footage/.venv/Scripts/python tools/movers/footage_boxes.py runs/footage/<run> --out android/app/src/test/resources/replay/drone-meva.csv

# 2. What the phone's tracker actually produced in a session: per id, lifetime, displacement, speed.
adb pull /sdcard/Android/data/dev.minband.android/files/gt-<unix>.csv .
python tools/movers/gt_summary.py gt-<unix>.csv

# 3. The session's own hits (every lifted detection) replayed through the tracker on the JVM.
adb pull /sdcard/Android/data/dev.minband.android/files/det-<unix>.csv .
MINBAND_DET_LOG=$PWD/det-<unix>.csv ./gradlew -p android :app:testDebugUnitTest --tests "*DetLogReplay*" -i

# 4. Why a track was tagged, without a replay: the phone logs every static -> moving flip with
#    the numbers behind it (displacement, drift subtracted, camera displacement, range, sigma, hits).
adb logcat -d -v time | grep "MinBand Pipeline" | grep "flip:\|moving:\|pose:"
```

A healthy session has one long-lived id per person (tens of seconds) with a median speed near
their walking speed. Many ids living ~1.5-2 s each is association failing between hits (seen on
the phone with the detector at 2-3 Hz before the tracker's gate followed the prediction's
uncertainty; see `android/README.md`, "Tracker at a slow detector").

## Limits, said plainly

- Latency to the figure: detector (90-150 ms on the Android phone) + tracker + link + poll
  (<= 50 ms at 20 Hz). Dead reckoning hides the poll, not the detector.
- Positions are where the tracker put the feet: ~0.1-0.3 m noise at 3-8 m; `ce` says how far
  to trust them when the link degrades.
- Heading comes from the velocity: a person turning on the spot keeps their last heading.
- The legacy Open3D visualizer has no text labels; the console prints spawns with gid and class.
