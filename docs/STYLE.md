# MinBand visual language

Reference: Oblivion-style credits poster, wireframe mountain contours, particle/mesh figure,
halftone-dithered statue. Monochrome, near-black, thin lines, thin type. No colour except where
noted. Applies to the viewer (laptop), the iOS app, and the eval charts.

## Tokens

| Token | Value | Use |
|---|---|---|
| `--bg` | `#070809` | page / scene background (never pure black, never grey cards) |
| `--ink` | `#e9ecef` | primary lines, values, titles |
| `--ink-2` | `#9aa3ad` | secondary text, labels |
| `--ink-3` | `#4a525b` | hairlines, grid, disabled |
| `--ink-4` | `#1a1e23` | faint grid, far contours |
| `--alert` | `#ffffff` at full opacity + blink, or `--ink-2` dashed | stale / lost; no red, no yellow |

Light theme is not supported; the product is dark by design.

## Lines

- Everything is a line. Entities, terrain, frustums, trails, boxes: 1 px (hairline) white or
  dim white, additive blending where cheap. Fills only as very low-alpha (0.04 to 0.08) washes.
- Density conveys importance: a fresh entity is drawn with more wire segments than a stale one;
  a stale entity decays to a dotted outline, then to a halftone dot cloud, then disappears.
- The ground is not a flat grid: a low-amplitude procedural contour field (displaced wireframe
  plane, like the mountain reference) at `--ink-4`, with the marker origin at the flattest point.
  Grid helper lines, if any, are `--ink-4` and sparse.
- Class is conveyed by silhouette, not colour: person (shown as "dismount") = tall capsule,
  carried object = small sphere, static object = box, vehicle (car, bus, truck) = a low car-sized
  box (4.4 x 1.8 x 1.5 m) turned to its heading, two-wheeler (bicycle, motorcycle) = a smaller
  one; the tag names the class (`car`, `truck`). Device is conveyed by line style: solid,
  dashed, dotted (frustums, trails, ghosts; never the error ring, see below).
- **Entities are solid, not wire** (decided 2026-10-09). Tracked objects are filled, matte
  monochrome meshes: `--ink` at 85 to 100 % with soft hemispheric lighting so they read as
  volumes against the line terrain. Fresh = bright solid; stale = the same solid fading toward
  `--ink-3`, then a dotted outline, then gone. Wire is for the environment (terrain, frustums,
  trails, axes), fill is for things the twin believes exist. That contrast is the point.

## Type

- Font: a light geometric sans (Inter 300/400 or system `-apple-system` with `.light` weight
  on iOS); monospace (IBM Plex Mono / SF Mono) for numbers only.
- Labels: uppercase, `letter-spacing: 0.18em`, 10 to 11 px, `--ink-2`. Values: `--ink`, mixed
  case or numerals, 12 to 14 px, weight 400. Title: `MINBAND` uppercase, weight 200 to 300,
  `letter-spacing: 0.3em`, 20 to 28 px.
- Credits layout: rows of `LABEL  Value` pairs across the top (edge device · link · twin), like a
  film poster's starring / music / direction row. No icons.
- Numbers never jump: fixed-width digits (`font-variant-numeric: tabular-nums`).

## Surfaces and controls

- No cards, no rounded corners, no drop shadows, no filled buttons.
- Panels are separated by 1 px hairlines in `--ink-3`. No inset frame around the page or
  screen and no `+` crop marks (dropped 2026-10-09); content runs to the edges.
- Buttons are uppercase text with a hairline underline; active state inverts (ink background,
  bg text) or shows a leading `▸`. Toggles read as `FUSION  ON`.
- Sliders: 1 px track in `--ink-3`, 2 px filled portion in `--ink`, square 8 px thumb.
- Graphs: oscilloscope look. 1 px trace in `--ink`, hairline grid in `--ink-4`, reference lines
  dashed in `--ink-2`, no area fill.
- Halftone: a dot-screen (4 to 6 px cell, dot radius from luminance) is the texture for any
  photographic or video content shown inside the product (the raw-video panel, placeholders,
  thumbnails). Video is monochrome and halftoned; the twin is lines. The contrast between the
  two is the point of the side-by-side.
- Motion: slow. Fades 300 to 600 ms, no bounces. Stale blink at 1 Hz, 40 to 100 % opacity.

## iOS specifics

- The camera feed stays in colour only while the operator is aiming; everything drawn over it is
  hairline white. Detection boxes are corner ticks (4 short lines), not full rectangles, with a
  label `PERSON · 03 · 0.82` in monospace above the top-left corner.
- A `WIREFRAME` mode hides the camera (black background) and shows the scene-reconstruction
  mesh (LiDAR) or feature points as white wires, plus the marker axes. This is the stage mode.
- HUD at the top in the credits layout: `ORIGIN  locked · LINK  1.2 kbps · TRACKS  3 · SEQ  412`.
  Controls at the bottom as uppercase text buttons separated by hairlines.

## Restraint (added 2026-10-09)

The product must be usable first. Less on screen, not more.

- **One primary readout per surface.** Viewer: bytes per second on the link. iOS: link state and
  track count. Everything else is secondary and visually quieter by two steps (size and tone).
- **Progressive disclosure.** The default view shows the twin, the link readout, the scenario
  selector and the twin-error number. Sliders, device statistics, the packet log, the bytes
  graph, ghosts and the side-by-side toggle live behind a single `DETAILS` disclosure (collapsed
  by default, remembered per browser). On iOS the HUD shows four items at most; detector rate,
  theta scale, wire bytes and sequence number go behind a long press or a `DETAILS` toggle.
- **Numbers are rounded to what a human needs:** kbps with one decimal, error in whole cm, no
  raw counters in the default view. Counters (deltas, keyframes, gaps, nacks) only in details.
- **Scene density caps.** Trails at most 3 s. One contour layer, not two, and it sits at
  `--ink-4` so entities are the brightest thing on screen. Velocity arrows only for entities
  moving faster than 0.3 m/s. No per-entity text labels in the 3D view by default; hover or tap
  shows one. Ghosts off by default.
- **Whitespace is part of the design.** Panels have 24 px padding, rows have 12 px gaps, and the
  credits row has at most three pairs. Nothing blinks except a lost device.
- **Motion is informative only.** No idle animation on the terrain, no pulsing, no scanlines.

## Trust, link and stage visuals (added 2026-10-09)

Element types for the hackathon visuals (docs/HACKATHON_PLAN.md section 5). Same tokens, same
hairlines; each moves only when a datagram arrived, a bit was delivered or a heartbeat was missed.

- **Trust states.** Live = bright solid. Coasting (the device missed its heartbeat, S15) = the
  same solid at about `--ink-2`, velocity arrow dimmed. Stale = the fade to `--ink-3`, dotted
  outline, gone, as above. Coasting does not blink; only a lost device does.
- **Error ring (V2).** Every entity stands on a 1 px ground ring of radius `ce` (metres), draped on
  the contour field. Its line style says trust, not device: live solid, coasting dashed (48 dashes
  at any size), stale dotted. Full opacity up to 1.5 m, then falling as sqrt(1.5 / r) to 0.3, so a
  blackout's wide rings stay quieter than the solids. A ring is drawn no larger than 12 m (the
  basin and the first swells): a larger one stays at 12 m with four short outward ticks, and the
  hover tag gives the real radius (`±24 m`). Shrinking snaps (a refresh resets the error); growth
  eases over about 0.2 s.
- **Tags** may carry a second line: the grid reference (MGRS) when the server has a geodetic anchor.
  The anchor's own reference sits in the credits row after the entity count.
- **Link activity strip (V3).** Vertical, newest at the top, 36 px per second, one 1 px tick per
  datagram, width proportional to bytes (1200 B = the lane). Uplink right of a hairline spine in
  `--ink`, downlink left in `--ink-2`, dropped datagrams dotted `--ink-3` with a small x, keyframes
  with a 5 px end cap. Beside it H.264 on the same scale, which is a solid bar (`--ink-3` at half
  opacity). A missed keyframe is the strip's one dashed rule (`NO KEYFRAME · DEV 101 102`), a
  resync a solid `--ink-3` rule, a dead link a 0.05 wash. Lives in DETAILS; STAGE moves it onto
  the stage.
- **Video on this link (V1).** Halftone painted top-down at the link rate, a 1 px `--ink-2` rule at
  the row being written, the previous frame below it at 12 % luminance. The countdown
  (`NEXT FRAME 1:52`) is the panel's one large number. The AI thumbnail is a 72 px square, hairline
  border, 6 px cells. With the link unshaped the panel is the live halftone and says so.
- **STAGE** is the presenter toggle (in DETAILS, remembered per browser, `?stage=1`): the link
  activity strip beside the twin and the video-on-this-link panel open. The operator view stays as
  "Restraint" describes. The per-datagram click (`CLICK`) is off by default.

## Wide area (added 2026-10-10)

Real drone footage spreads entities over 50 to 200 m; the contour field is drawn for a room around
the marker. The viewer switches to a wide mode when the 90th-percentile distance of the entities
from their centroid stays above 15 m (back below 8 m for 2.5 s), and nothing else changes in the
room mode.

- **Ground.** A flat hairline grid at `--ink-4` replaces the contour field, 10 cells to the scale
  radius (20, 50, 100, 200 ... m from the entities' extent), fading out radially and into the fog.
  The key adds the spacing (`10 M GRID`). Rings lie on it.
- **Glyphs are map symbols.** Each class keeps a minimum size on screen (a dismount about 26 px
  tall, a vehicle about 24 px long), magnified from true size only as far as needed and never
  shrunk; zooming in returns them to true size. Vehicles turn to their heading. Velocity arrows
  start at the glyph's edge and show one second of travel, between 12 and 90 px. Dash patterns keep
  their room length on screen. **Error rings stay true metres** (they are error bars, not
  symbols), capped at half the scale radius with the same outward ticks.
- **Devices.** The frustum keeps about 44 px of depth at its own distance, and a dotted drop to a
  small ground cross marks the drone's nadir; the hover tag adds its height (`DEV 100 · ALT 59 M`).
- **Framing.** When the mode starts the camera glides once (600 ms) onto the entities from where
  the drone looks, about 55 degrees down, and follows the bounds while the feed fills in over the
  first 2.5 s. After that it never moves on its own: `F` or the `FRAME` text control (top right
  of the twin, wide mode only) re-frames from the operator's bearing. Touching the orbit ends any
  glide.

## Charts: colour for series (added 2026-10-09)

Exception to monochrome: in the eval charts and the viewer's graphs, series are told apart by
colour, not by dash pattern. Use a small muted categorical palette on the dark background,
one hue per scenario, consistent across every chart: e.g. `#8ab4f8` (blue), `#f28b82` (red),
`#81c995` (green), `#fdd663` (yellow), `#c58af9` (purple). Lines stay 1 px; axes, grid and
labels stay monochrome. Reference lines (baselines, caps) stay dashed in `--ink-2`.
