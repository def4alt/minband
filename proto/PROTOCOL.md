# MinBand wire protocol v0

Transport: UDP, edge -> server on :7777 (same socket for server -> edge acks). One message per
datagram, max 1200 bytes payload (safe below typical MTU; a keyframe that doesn't fit is split
into several `Keyframe` datagrams with `part/of`).

Encoding: `postcard` (serde) of the Rust types in `core/src/wire.rs`. Varints for integers,
f32 LE for floats. Every message starts with `version: u8 = 0` then a `kind: u8`.

Time: `tick: u32`, 1/120 s since the edge session start. Sequence: `seq: u32` per device, per
datagram, wraps.

## Messages

| kind | name | dir | fields |
|---|---|---|---|
| 0 | Hello | E->S | device_id: u32, session_nonce: u32, caps: u8 (bit0 depth, bit1 thumbnails), tick |
| 1 | Delta | E->S | seq, tick, updates: Vec<Update> |
| 2 | Keyframe | E->S | seq, tick, part: u8, of: u8, entities: Vec<EntityState> |
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

Size: an `Update` is 1 + 1(id) + 1 + 12 + 12 + 1 + ~3 = ~31 bytes; a `Delta` with one update plus
headers and UDP/IP is ~70 bytes on the wire.

## Behaviour summary

- Edge sends `Hello` until it receives any `Ack`, then starts `Delta`/`Keyframe`/`Pose`.
- Every `Delta` and `Keyframe` consumes one `seq`. `Pose` also consumes one (so gaps are
  detectable) but is never repaired.
- Server acks every 100 ms or immediately on a gap. `missing` lists seqs not received; the edge
  resends current state for entities it touched in those seqs (state repair, see DESIGN §4), or a
  `Keyframe` if more than 8 seqs are missing.
- `budget_bps` in `Ack` lets the server (or the operator slider) push a byte budget to the edge.
  `0` means "unlimited".
