# MinBand wire protocol v1

Transport: UDP, edge -> server on :7777 (same socket for server -> edge acks). One message per
datagram, max 1200 bytes payload (safe below typical MTU; a keyframe that doesn't fit is split
into several `Keyframe` datagrams with `part/of`).

Encoding: `postcard` (serde) of the Rust types in `core/src/wire.rs`. Varints for integers,
f32 LE for floats. Every message starts with `version: u8 = 1` then a `kind: u8`. The version byte
is checked before the body: a peer on another version fails with `BadVersion(v)`, not `Malformed`.

Time: `tick: u32`, 1/120 s since the edge session start. Sequence: `seq: u32` per device, per
datagram, wraps.

## Messages

| kind | name | dir | fields |
|---|---|---|---|
| 0 | Hello | E->S | device_id: u32, session_nonce: u32, caps: u8 (bit0 depth, bit1 thumbnails), tick |
| 1 | Delta | E->S | seq, tick, theta_q: u8, updates: Vec<Update> |
| 2 | Keyframe | E->S | seq, tick, theta_q: u8, part: u8, of: u8, entities: Vec<EntityState> |
| 3 | Pose | E->S | seq, tick, pos: [f32;3], quat: [f32;4] (unit, w last), origin_locked: bool |
| 4 | Ack | S->E | last_seq: u32, missing: Vec<u32> (seqs, at most 32), budget_bps: u32 |
| 5 | Bye | E->S | seq, tick |

`Update` is an enum:

| tag | name | fields |
|---|---|---|
| 0 | Spawn | EntityState |
| 1 | Update | EntityState |
| 2 | Despawn | id: u32, tick |

`EntityState`:

| field | type | note |
|---|---|---|
| id | u32 | per-device local id; global id = (device_id, id) |
| class | u8 | COCO index subset, see `core/src/classes.rs` |
| pos | [f32;3] | metres, marker frame, Y up |
| vel | [f32;3] | m/s |
| conf | u8 | 0..255 |
| tick | u32 | when this state was observed |

Sizes (measured in `core/src/wire.rs` `typical_sizes`; id < 128, tick >= 16 384 i.e. after 136 s,
which takes a 3-byte varint, 2 bytes before):

| item | payload | on the wire (+28 B UDP/IPv4) |
|---|---:|---:|
| `Update` (tag + `EntityState`) | 31 B | |
| `EntityState` in a keyframe | 30 B | |
| `Despawn` | 5 B | |
| `Delta` with one update | 40 B | 68 B |
| `Keyframe` with n entities | 11 + 30n B | 39 + 30n B |

The naive 30 Hz baseline (`tools/eval`, server metrics) uses the same numbers: 31 B per entity and
40 B per message including the 28 B header.

### theta_q (v1)

The position threshold the edge was using when it sent the datagram, θ_pos x `theta_scale`
(budget controller): while the link is good, the twin is within this distance of the edge's
track. One byte, a minifloat in centimetres with a 3-bit exponent `e` and a 5-bit mantissa `m`:
`m` cm for `e = 0`, else `(32 + m) << (e - 1)` cm. 1 cm steps up to 63 cm, then steps of ~3 %, max
4032 cm (40.32 m). The edge rounds up, so the declared value is never below the threshold in use
(0.15 m is exactly 15). The receiver keeps, per entity, the `theta_q` of the datagram that last
refreshed it (`Extrapolated.theta`, metres).

## Behaviour summary

- Edge sends `Hello` until it receives any `Ack`, then starts `Delta`/`Keyframe`/`Pose`.
- Every `Delta` and `Keyframe` consumes one `seq`. `Pose` also consumes one (so gaps are
  detectable) but is never repaired.
- Server acks every 100 ms or immediately on a gap. `missing` lists seqs not received; the edge
  resends current state for entities it touched in those seqs (state repair, see DESIGN §4), or a
  `Keyframe` if more than 8 seqs are missing.
- `budget_bps` in `Ack` pushes a budget (bit/s on the link, UDP/IP header included) to the edge.
  It is authoritative, `0` included (`0` = unlimited): the edge always runs the cadence below for
  the budget the receiver last advertised, and the receiver derives its liveness thresholds from
  the same number. `Edge::set_budget` applies until the next `Ack`.
- The keyframe is the heartbeat: it is sent on schedule even when nothing is tracked (an empty
  keyframe, `of = 1`), so silence always means a lost link.

### Cadence from the budget

`core/src/cadence.rs` `cadence(budget_bps)`, integer math, identical on both ends. Keyframe period
= 1 s + the time the budget needs for 1 kB (clamped to 2..15 s); Hello refresh = 2 keyframes
(5..30 s); pose interval ~6 % of the budget at >= 4 kbit/s (0.5..2 s), 10 s below; coast = one
keyframe period + max(25 %, 0.5 s); stale = max(6 s, 3 keyframes); drop = max(10 s, 5
keyframes). The edge's age cap (`t_max`) is 1.5 keyframe periods, so it never pre-empts the
keyframe.

| budget bit/s | keyframe | hello | pose | coast | stale | drop |
|---|---|---|---|---|---|---|
| 0 (unlimited), >= 16000 | 2 s | 5 s | 0.5 s | 2.5 s | 6 s | 10 s |
| 8000 | 2 s | 5 s | 1 s | 2.5 s | 6 s | 10 s |
| 4000 | 3 s | 6 s | 2 s | 3.75 s | 9 s | 15 s |
| 1500 | 6.33 s | 12.7 s | 10 s | 7.9 s | 19 s | 31.7 s |
| 600 | 14.3 s | 28.7 s | 10 s | 17.9 s | 43 s | 71.7 s |
| <= 571 | 15 s | 30 s | 10 s | 18.75 s | 45 s | 75 s |

`Pose` is gated in the core: `Edge::pose` returns nothing when called sooner than the pose
interval after the last pose (one 30 Hz frame of slack), so callers call it at their own rate.

### Keyframe pacing (budget > 0)

At budget 0 a keyframe goes out in one tick as one snapshot, split only by `MAX_DATAGRAM`. At any
other budget it is paced like a video intra-refresh, so its parts never queue up behind each other
in a small radio buffer:

- When the keyframe starts (its `tick`), the entity list is fixed and split into parts of at most
  2 s of link time at the budget (header included; at least one entity, at most 64 parts).
- Part 0 goes out at once; each next part one link time of the previous part later at the budget
  (at least 100 ms). If the budget is lifted to 0 meanwhile, the rest go out at once.
- Every part carries the keyframe's `tick` (it names the keyframe and is the reconciliation
  reference) and the *current* state of its entities, sampled when the part is sent (their
  `EntityState.tick`). Entities that vanished meanwhile are left out (they get a `Despawn`).
- Deltas keep flowing between parts. The edge resets an entity's ghost only when its part is sent.
- The receiver applies each part like a delta (newer `EntityState.tick` wins, so neither a delta
  nor a part can regress newer state) and, once every part has arrived, removes entities the
  keyframe did not list and that were not observed after its `tick`. A late part of an older
  keyframe never discards a newer keyframe's progress. A datagram's edge time, for coasting and
  clock sync, is the newest tick it carries.
- A periodic keyframe waits for a paced one still in flight; a forced one (nack) replaces it.

### Coasting and error radius (receiver)

A device is *coasting* when (edge time now - edge tick of its last datagram) >= `coast_ticks` of
the advertised budget. Per entity the receiver reports `theta` (declared threshold, m), `coasting`
and `ce` (m): `theta` while not coasting, else `theta + max_speed(class) x silence` with silence
counted from the device's last datagram (a missed heartbeat is a visible jump), capped at 1000 m.

Trust comes back per entity. The first datagram after a silence of at least `coast_ticks` ends the
device's coasting, but updates lost during the blackout are repaired only a round trip later (nack)
or by the next keyframe. So every entity held at that moment stays coasting, its `ce` still growing
from the silence start (the edge tick of the last datagram before it), until a datagram sent after
the resume refreshes it: a delta update or repair, or a keyframe part listing it (an older state
that the receiver rejects does not count). A complete keyframe whose `tick` is at or after the
resume clears every entity that remains; entities first seen after the resume are trusted at once.
If another blackout comes before an entity is refreshed, its silence keeps counting from the first.
`stale` = entity age >= `stale_ticks`. `gc` drops an entity once the device has been silent for
`stale_ticks` and the entity is `drop_ticks` old, or regardless after max(30 s, 3 x `drop_ticks`).
When the advertised budget rises (tighter limits) the previous limits still apply for one old
coast period, since the edge has not heard of the new budget yet.

## Peeking at a datagram

`peek_json(bytes)` (WASM; `wire::peek` in Rust) returns
`{"kind":"hello|delta|keyframe|pose|ack|bye|malformed", "deviceId"?, "nonce"?, "seq"?, "tick"?,
"ids"?, "part"?, "of"?, "thetaM"?, "error"?}`: `ids` are the entity ids a delta (spawns, updates,
despawns) or keyframe part carries, `tick` the datagram's send tick (for a paced keyframe part its
newest entity tick), `error` e.g. `"Malformed"` or `"BadVersion(0)"`. `describe(bytes)` is the
one-line human-readable form.
