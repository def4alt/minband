# Side-by-side: edge truth vs what the receiver decoded

A browser test page for the v2 protocol. Left: the real MEVA drone clip with the edge's tracks
and contacts drawn on the video. Right: what a receiver holds after decoding the wire (top-down
map, derived events, the frames themselves, bytes/s). One WebSocket feeds both halves. `src/driver.ts` is
the real thing: the footage tracks go through the Rust/WASM edge (`core/`), real wire frames, a
shaped link and the Rust/WASM receiver. `src/mock.ts` is a stand-in that fakes the right side, kept
for page work without the core.

```
cd tools/sidebyside
npm install
(cd ../../core && wasm-pack build --target nodejs --out-dir pkg-node --release -- --features wasm)
npm start                 # real driver, http://localhost:8090   (PORT=, RUN_DIR=, VIDEO=, PROFILE= to override)
npm run mock              # fake right side, same page
npm run check             # numeric projection check + runs/sidebyside/check-points.json
npm run smoke             # endpoints + WebSocket contract against a running server
npm run shot              # headless screenshot (uses ../../viewer's playwright if installed)
```

Prerequisites (both gitignored under `runs/`):

- `runs/footage/meva-2018-03-13.16-00-14-bf/{tracks.csv,detect.json,summary.json}` from
  `tools/footage/track.py` (the run dir is `RUN_DIR`).
- the browser-playable clip window, clip time 15.015 s = tick 0:

  ```
  ffmpeg -ss 15.015 -t 90.1 -i tools/footage/clips/2018-03-13.16-00-14.16-03-38.uav1.mp4 \
    -vf scale=1280:-2 -c:v libx264 -preset veryfast -crf 23 -an -movflags +faststart \
    runs/sidebyside/meva-720p.mp4
  ```

  `-ss` before `-i` is accurate-seek by default: the first output frame is source frame 450
  (checked with ffprobe: pts 0.000, then 1/29.97 steps), so `video.currentTime = tick / 120`.

Visual check of the projection: `npm run check 2400 && tools/footage/.venv/bin/python
scripts/draw-check.py` paints the tracks of that tick on the matching 720p frame ->
`runs/sidebyside/check-overlay.jpg`.

## What is real and what is not

Real: the clip, the detections and tracks (`tools/footage` on the same clip), the contact
extraction, grouping and revisions (Rust `contacts.rs`), the frames (byte-exact `wire.rs`), the
schedule (`scheduler.rs`, `edge.rs`), the receiver's merge, dead reckoning, liveness and derived
events (`receiver.rs`), the digests and focus commands on the uplink. The link is shaped in the
driver with the hackathon's profiles (rate as the edge's budget, one-way delay, random loss,
`contested` adds 1-5 s blackouts, `jam` cuts both ways). Not real: the drone's own telemetry (the
clip ships none): camera height and pitch are the tracker's fit on this footage, the position is
the camera nadir, heading north, loiter, GNSS fix, battery draining, origin lat/lon = Muscatatuck.

## Layout

- Left: `<video>` + canvas overlay. Edge tracks = class-coloured dots (dismount yellow, vehicle
  cyan, armour red, other grey), dead-reckoned with `ve, vn` between messages. Edge contacts =
  circles of `radius` (projected: centre and a point `radius` m east), label `#id count mix`,
  motion arrow, focused one in thick yellow, unconfirmed dashed. "rx belief on video" draws the
  receiver's contacts on the same picture in magenta dashes with the `ceShown` ring and liveness.
- Right top: top-down, north up, 50 m grid, drone glyph (heading) and footprint at the rx ego
  (edge ego faint until the receiver has heard one), rx contacts with ce rings, count, mix,
  motion, course arrow; liveness as opacity/dash (fresh, unheard, lost, departed). Edge
  contacts/tracks as faint ghosts (toggle). Click a contact: `focus track` + a small menu for
  split / chip / release.
- Right middle: events, newest at the bottom, `t=12.3 [kind] text`, coloured by kind, last 200.
- Right bottom: the wire log, one row per frame (`seq 41 · 84 B · delivered · Ego, Contact ×3`,
  expandable to the lines), bytes/s sparkline over the last 60 s with the budget as a dashed
  line, and the readout `budget · B/s · k of n known · profile`.
- Controls: play/pause (space), seek slider (sends `seek`), rate 1/2/4x, link profiles, `jam`
  = blackout (press again to restore the previous profile). The server owns play state: the
  video follows `clipT` (re-synced when off by > 0.25 s) and plays/pauses with the feed.

The page is plain ES modules (`web/app.js`, `web/camera.js`), no build step; `web/` is served
as static files by whatever process owns the WebSocket.

## WebSocket contract (server -> page), one JSON message every 100 ms

```
{ "t": 12.3,                      // replay seconds since start = tick/120
  "clipT": 12.3,                   // seconds into the 720p file (same as t)
  "playing": true, "rate": 1,      // OPTIONAL extension; without them the page infers play state from t
  "edge": {                        // ground truth as the edge sees it
    "tracks":   [{ "id": 7, "cls": 2, "e": 12.1, "n": -3.4, "ve": 0.1, "vn": 0.0, "conf": 200 }],
    "contacts": [{ "id": 3, "rev": 5, "e": 10.0, "n": -2.0, "ce": 6.0, "radius": 12.0,
                   "count": 4, "mix": {"dismount":0,"vehicle":4,"armour":0,"other":0},
                   "motion": "static|moving|stopped|unknown", "confirmed": true, "lost": false,
                   "departed": false, "focused": false, "course": 90, "speed": 3.2,
                   "members": [7, 9, 11, 12], "firstSeen": 3.1, "since": 8.0 }],
    "ego": { "e": 0, "n": 0, "altAgl": 80, "heading": 0, "speed": 0, "nav": "loiter",
             "gnss": "fix", "link": "hears", "battery": 83, "fpE": 0, "fpN": 10, "fpRadius": 60 } },
  "wire": {                        // frames built since the last message
    "frames": [{ "seq": 41, "bytes": 84, "delivered": true, "lines": ["Ego ...", "Contact id=3 rev=5 ..."] }],
    "budgetBps": 800, "profile": "lora", "up": true, "bytesPerS": 63.5, "dropped": 2 },
  "rx": {                          // what the receiver holds
    "contacts": [{ ...same fields as edge.contacts..., "ceShown": 9.5, "liveness": "fresh|unheard|lost|departed", "ageS": 4.2 }],
    "ego": { ...same as edge.ego..., "ageS": 2.0 } | null,
    "events": [{ "t": 11.9, "kind": "new|confirmed|moving|stopped|static|grew|shrank|lost|departed|split|merged|ego", "id": 3, "text": "..." }],
    "known": 7, "of": 9, "bytesTotal": 12345 } }
```

Conventions the page relies on:

- ENU metres: east = tracks-frame x, north = -z, up = y; origin = the tracks-frame origin
  (the median detection position of the run, see below). Course in degrees clockwise from north.
- `rx.events` and `wire.frames` are the ones raised since the previous message (deltas); the
  page accumulates them and keeps the last 200 of each. A `t` that jumps backwards by more than
  0.5 s (seek, loop) clears both logs and the sparkline.
- `rx.ego` is `null` until the receiver has heard an Ego; the map then uses the edge ego, faint.
- The event `kind` colours are for the listed kinds; unknown kinds render in the default colour
  (the mock also emits `focus` on focus commands).
- Class -> mix: 0 dismount; 101 armour; 100 other (motion-only mover); everything else vehicle.

Page -> server commands:

```
{"cmd":"link","profile":"clean|hf|lora|telemetry|contested|blackout"}
{"cmd":"focus","id":3,"mode":"track|split|chip|release"}
{"cmd":"pause"}  {"cmd":"play"}  {"cmd":"seek","t":30}  {"cmd":"rate","x":1}
```

HTTP on the same port: `/` and the files of `web/`; `/video.mp4` (must honour `Range`, the
browser seeks with it); `/meta` (below); `/state` (debug).

## Projection chain (`web/camera.js`)

`/meta` is built from the run dir (`scripts/meta.mjs`):

```
{ fps, width, height, start_frame, end_frame, every,
  ground: { f_px, pitch_deg, height_m, cx, cy },   // summary.json.ground, cx = width/2, cy = height/2
  origin: [ox, oy],                                 // = [-camera_m[0], camera_m[2]]
  homographies: { "450": [[...3x3]], "456": ..., } } // detect.json: frame n -> reference (frame 450)
```

`projectEN(e, n, clipTimeS) -> {u, v} | null` in pixels of the original 3840x2160 clip (scale by
the displayed size):

1. ENU -> tracks frame: `x = e`, `z = -n`.
2. tracks -> ground model. `track.py` writes `x = X - origin[0]`, `z = -(Y - origin[1])` with
   `origin = median(detection XY)` and `summary.camera_m = [-origin[0], h, origin[1]]`, so
   `X = x + origin[0] = e - camera_m[0]`, `Y = -z + origin[1] = n + camera_m[2]`. For this run
   `origin = [-23.264, 16.138]`.
3. ground -> reference pixel: `track.py`'s `camera_centres` matrix
   `G = K [r1 r2 t0]`, `K = [[f,0,cx],[0,f,cy],[0,0,1]]`, `R0 = [[1,0,0],[0,-sin p,-cos p],[0,cos p,-sin p]]`,
   `t0 = -R0 [0,0,h] = [0, h cos p, h sin p]`, so `(u, v, w) = G (X, Y, 1)`, `u/=w, v/=w`. This is the
   exact inverse of `Ground.to_ground` (round trip 1e-12 px, `npm run check`).
4. reference -> current frame: `inv(H_n)`, with H normalised to `H[2][2] = 1` and the inverse
   linearly blended between the registered frames either side of
   `frame = start_frame + clipTimeS * fps` (every 6th frame has an H; 0.2 s of drone drift is a
   few pixels, so the blend matters at the frame edges).

`unprojectPixel(u, v, clipTimeS) -> {e, n}` is the inverse for click handling.

Verified numerically (`scripts/check-projection.mjs`) and visually: `scripts/draw-check.py` on
the 720p frame at tick 2398, and a video+overlay composite taken from the live page at t = 40.5 s;
dots sit on the tyres of the parked cars and on the pedestrians (the tracker uses the bottom of
the detection box as the ground contact).

## What the mock fakes

`src/mock.ts` replays `tracks.csv` in real time (10 Hz, pause/seek/rate, loops at 90 s) and:

- `edge.tracks`: straight from the CSV (ENU: `e = x`, `n = -z`, `ve = vx`, `vn = -vz`).
- `edge.contacts`: tracks clustered within 20 m with sticky membership (a member leaves at 26 m)
  and stable ids; count, mix by class, radius = farthest member + 2 m (min 5), motion from the
  mean velocity (moving > 0.5 m/s, stopped < 0.3 m/s, static after 10 s stopped), course, speed;
  confirmed after 1 s; lost when no members (departed if it was moving), removed 3 s later;
  `rev` bumps on count/motion change or a 3 m move; `ce = 3 + 0.5 speed`.
- `edge.ego`: fixed at the camera nadir (`camera_m`), heading 000, 80 m AGL, loiter, battery
  draining, footprint 74 m around the image centre.
- `wire`: one frame per link period (clean 0.5 s, telemetry 0.2, hf 2, lora 1, contested 1)
  with an Ego line, the contacts whose rev changed since last sent, lost/departed notices, and
  one round-robin refresh, trimmed to `budgetBps * period`; 14 B per contact line. Dropped at
  the profile's loss (clean 0, telemetry 1 %, hf 2 %, lora 5 %, contested 30 %, blackout all)
  and delivered after its delay (0.1-2 s); undelivered changes are re-sent in the next frame.
- `rx`: the delivered frames applied after the delay; `ceShown = ce + 1 m/s * ageS`; liveness
  fresh < 3 s < unheard, lost/departed from the notices, dropped after 10 s (20 s if the edge
  lost the contact and the notice never arrived).
- `rx.events`: derived from rx state changes (new, confirmed, grew/shrank, moving/stopped/static,
  lost/departed) plus an `ego` line every 10 s and `focus` on focus commands. No split/merge.
- `focus`: `track` sets `focused` on the edge contact (and bumps its rev so it goes out);
  `release` clears it; `split`/`chip` only log an event.

Not in the mock: real framing/bytes, chip images, split/merge, GNSS/link state changes, ego motion.
