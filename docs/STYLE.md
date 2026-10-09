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
- Class is conveyed by silhouette, not colour: person = tall capsule, carried object = small
  sphere, static object = box. Device is conveyed by line style: solid, dashed, dotted.

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
- Panels are separated by 1 px hairlines in `--ink-3`. A page or screen has a 1 px inset frame
  with small `+` crop marks in the four corners (see poster).
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
