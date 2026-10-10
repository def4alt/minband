# MinBand wire protocol v2: contact reports over a trickle link

Status: draft 2026-10-10, written after the pivot interview (see `docs/DESIGN_V2.md` for the
reasoning, the event model and the research behind the durability choice). Normative where it
says MUST. Byte layouts are exact so a C implementation on a flight controller can speak it.

v1 (`PROTOCOL.md`) streams per-entity state deltas in a marker frame with centimetre thresholds
and repairs loss with acks. v2 streams **self-contained contact reports** in a geodetic frame at
the sensor's real error scale, repeats them on a priority schedule so a receiver can rebuild the
picture from any subset of frames, and treats the uplink as optional.

## 0. Design rules (what every message obeys)

1. **Self-contained.** Every frame decodes on its own. A receiver needs exactly one `Session`
   record (repeated) to turn any later record into lat/lon. No record refers to an earlier frame.
2. **Idempotent, order-free.** A record is a full state, keyed by id and revision. Applying it
   twice, late, or out of order gives the same world (last-writer-wins by `(rev, tick)`).
3. **History travels inside state.** A contact record carries when it was first seen and when its
   current motion state began, so a receiver that missed the transition still reconstructs the
   event with the right time. Events are derived, never sent as one-shot messages.
4. **Repetition is the durability mechanism.** Changed records are repeated on a doubling ladder,
   unchanged ones at a slow floor, so a blackout of any length shorter than the floor loses nothing
   once the link returns. The uplink only *suppresses* repeats and *steers* attention.
5. **Bytes follow surprise.** A record's revision moves only when the receiver's dead reckoning of
   it would be wrong by more than its own error radius, or when its state machine changes.
6. **Transport-neutral.** A frame is bytes with a CRC. Carriers: UDP (demo, Wi-Fi, LTE), serial
   with COBS framing (companion computer to radio), MAVLink `TUNNEL` (128 B payload), CRSF custom
   telemetry frames (about 60 B). The frame size limit is a link parameter.

## 1. Frame

```
offset  size  field
0       1     magic      0xB2 (protocol v2). A v1 datagram starts with 0x01 and is rejected.
1       2     session    low 16 bits of the session nonce (u16 LE)
3       2     seq        per sender, per frame, wraps (u16 LE)
5       4     tick       sender time, 1/120 s since session start (u32 LE)
9       1     flags      bit0 uplink (receiver -> edge) ; bit1 crc present ; bit2 cycle end
                         (the last frame of a full pass over live records) ; bits 3-7 reserved 0
10      ...   records    type u8, len u8, body[len] ; repeated until the frame ends
end-2   2     crc16      CRC-16/CCITT-FALSE over bytes 0..end-2, present when flags.bit1
```

- Max frame size is a link parameter: 1200 B on UDP, 128 B on MAVLink tunnel, 60 B on CRSF.
  The edge picks a *target* frame size of about one second of link time at the budget (64 B at
  100 B/s), never above the limit: a lost frame then costs about a second, not a burst.
- `len` lets a receiver skip record types it does not know. Unknown types MUST be skipped, never
  treated as an error.
- `tick` is the time the frame was built; records carry their own ages relative to it.
- CRC is mandatory on serial carriers and optional on UDP/IP (which has its own checksum).

Header: 10 B (12 with CRC). Record overhead: 2 B.

## 2. Units and small encodings

| name | encoding | range, step |
|---|---|---|
| `pos` (dx, dy) | i16 LE each, east and north from the session origin, in `pos_res` units | ±32 km at 1 m, ±3.2 km at 10 cm, ±327 m at 1 cm |
| `m8` (metres minifloat) | u8: e = q>>5, m = q&31; `m/4` m for e = 0, else `(32+m)/4 * 2^(e-1)` m | 0..7.75 m by 0.25; 8..15.75 by 0.25; 16..31.5 by 0.5; 32..63 by 1; 64..126 by 2; 128..252 by 4; 256..504 by 8; 512..1008 by 16. Round up (the declared radius is never below the true one) |
| `course` | u8, true bearing × 256/360 | 1.4° |
| `speed` | u8, m/s × 4 | 0..63.75 m/s |
| `climb` | i8, m/s × 4 | ±31.75 m/s |
| `heading` | u8, × 256/360 | 1.4° |
| `secs` | u16, seconds since session start, saturating | 18.2 h |
| `age` | u8, seconds before the frame's `tick`, saturating | 0..255 s |
| `latlon` | i32 LE, degrees × 1e7 | 1.1 cm |

## 3. Downlink records (edge -> receiver)

### 3.1 `Session` (type 0x01, body 24 B)

Repeated every `T_session`; three times in the first 10 s of a session. Without it a receiver can
still show relative positions and counts, but not lat/lon.

```
u32  nonce          full session nonce (a new nonce = new origin, new ids, new tick 0)
u16  device_id
i32  origin_lat     latlon ; 0 with frame=1 (local)
i32  origin_lon     latlon
i16  origin_alt     metres above the WGS84 ellipsoid ; 0x7FFF unknown
u8   pos_res        0 = 1 cm, 1 = 10 cm, 2 = 1 m, 3 = 10 m
u8   frame          0 = geodetic ENU at origin (dx east, dy north) ; 1 = local (marker) frame,
                    receiver applies its own anchor as in v1
u16  caps           bit0 gnss, bit1 baro, bit2 mag, bit3 imu, bit4 rangefinder, bit5 thermal,
                    bit6 gimbal, bit7 chips, bit8 utc clock, bit9 visual odometry,
                    bit10 uplink expected (edge listens), bits 11-15 reserved
u32  utc_at_tick0   UTC seconds at tick 0 ; 0 unknown (then the receiver stamps on arrival)
```

### 3.2 `Ego` (type 0x02, body 26 B)

The drone's own state, the sensor footprint, and a one-line scene summary. Replaces v1 `Pose`.
Repeated every `T_ego`; sent at once when `nav`, GNSS state or link state changes.

```
i16  dx, dy         pos (own position)
i16  alt_agl        metres above take-off / ground ; 0x7FFF unknown
u8   heading
u8   speed          ground speed
i8   climb
u8   nav            bits 0-2 mode: 0 manual, 1 auto mission, 2 loiter, 3 return home, 4 landing,
                    5 failsafe, 6 lost link autonomy, 7 other
                    bits 3-4 gnss: 0 none/denied, 1 degraded or dead-reckoned, 2 3D fix, 3 RTK
                    bits 5-6 link: 0 hears uplink, 1 uplink silent > 30 s, 2 never heard uplink
                    bit 7 reserved
u8   battery        percent ; 255 unknown
u8   pos_ce         m8, own horizontal position uncertainty (1 sigma)
i16  fp_dx, fp_dy   pos, centre of the camera's ground footprint (where it is looking)
u8   fp_radius      m8, footprint radius (coverage: "scanned here, found nothing" is information)
u8   n_contacts     live contacts (groups and singles) the edge currently holds
u8   n_moving       of which moving
u8   n_dismount     dismounts in all contacts (sum of counts)
u8   n_vehicle      vehicles (car, truck, bus, motorcycle, bicycle, armoured)
u8   n_armour       of which armoured (class 101, only from an appearance model)
u8   n_other        motion-only movers and anything else
```

`n_*` is the integrity check: a receiver knowing fewer live contacts than `n_contacts` shows
"k of n contacts known" instead of pretending the picture is complete.

### 3.3 `Contact` (type 0x03, body 21 B, +2 velocity, +2 parent, +2 altitude)

One thing or one group of things on the ground. A single object is a contact with `count = 1`.
Individuals inside a group are sent only under focus (`parent` set).

```
u16  id             per session ; never reused ; a group keeps the id of its oldest member group
u8   rev            revision, wraps ; LWW key with tick (see 5.1)
u8   flags          bits 0-1 motion: 0 unknown, 1 static, 2 moving, 3 stopped (was moving, < 30 s)
                    bit2 confirmed (>= 3 looks or >= 2 s)
                    bit3 lost (no look for T_lost ; position is the last known)
                    bit4 departed (tombstone: the edge no longer tracks it ; kept for 2 x T_floor)
                    bit5 focused
                    bit6 group (count > 1 ; children available on focus)
                    bit7 has velocity (course, speed follow)
u8   ext            bit0 has parent (u16 follows) ; bit1 has altitude (i16 follows, metres above
                    origin) ; bit2 thermal-only sighting ; bit3 motion-only (no appearance class) ;
                    bit4 operator verified ; bit5 child record (individual within a group) ;
                    bits 6-7 reserved
i16  dx, dy         pos, centroid of the contact's ground footprint
u8   ce             m8, horizontal error radius of `pos` (1 sigma): geometry + own position error
u8   radius         m8, extent of the group around the centroid ; 0 for a single
u8   n_dismount     counts by coarse class ; count = the sum (1..255)
u8   n_vehicle
u8   n_armour
u8   n_other
u8   conf           0..255, detector/tracker confidence of the contact as a whole
u16  first_seen     secs ; when the first member was first detected
u16  since          secs ; when the current motion state began
u8   age            seconds since the last look at it, before the frame tick
[u8  course, u8 speed]   when flags.bit7 ; course over ground and speed of the centroid
[u16 parent]             when ext.bit0 ; the group this child belongs to
[i16 dz]                 when ext.bit1
```

### 3.4 `ChipHead` (type 0x04, body 10 B) and `ChipSym` (type 0x05, body 4 + S B)

An image crop of one contact, only on operator request (`Focus` with chip), sent as a systematic
RaptorQ (RFC 6330) object so a few lost symbols do not cost a full resend. Lowest priority: fills
whatever the budget leaves, capped at 50 % of it.

```
ChipHead:  u16 contact ; u8 chip ; u8 fmt (0 JPEG grey, 1 JPEG colour) ; u8 w ; u8 h ;
           u16 size (bytes) ; u8 K (source symbols) ; u8 S (symbol size, bytes)
ChipSym:   u16 contact ; u8 chip ; u8 esi (encoding symbol id: 0..K-1 source, K.. repair) ; S bytes
```

`ChipHead` is repeated every 8 symbols. A receiver decodes after any K (+2 for < 1e-6 failure)
distinct symbols. Without feedback the edge sends K + ceil(K/2) symbols and stops; with a `Digest`
saying `chip done` it stops at once. A 64x64 grey JPEG is about 1.5 kB: 48 symbols of 32 B, about
25 s of a 100 B/s link at the 50 % cap, 3 s at 1 kB/s.

### 3.5 `Note` (type 0x06, body 1 + n B, n <= 40)

`u8 kind` (0 edge status text, 1 operator text relayed, 2 geofence/boundary name) + UTF-8 bytes.
Rare, repeated on the ladder like a changed contact.

## 4. Uplink records (receiver -> edge), `flags.bit0 = 1`

All uplink records are also self-contained and the receiver repeats them on its own schedule
(the uplink is as lossy as the downlink). An edge on a simplex link never sees them and MUST work
without them.

### 4.1 `Digest` (type 0x81, body 5 + 3n B)

```
u16  last_seq       highest downlink seq seen
u16  budget         bit/s / 10 (0 = unlimited, as v1's budget_bps) ; authoritative while heard
u8   n              0..32
n x (u16 id, u8 rev)   contacts the receiver holds at this revision (most recently changed first)
```

Edge: a contact whose `(id, rev)` is in a digest is *acked*; its repeat interval jumps to
`T_floor` (or it leaves the rotation if it is a tombstone). The edge keeps the ladder for anything
not acked. Digests go every 5 s while frames arrive, at once when a contact changes revision.

### 4.2 `Focus` (type 0x82, body 5 B)

```
u16  id             a contact
u8   mode           bit0 track (repeat every T_focus, halve the change threshold)
                    bit1 split (send the group's children as Contact records with parent)
                    bit2 chip (send one ChipHead/ChipSym object for it, at chip_px)
                    bit3 release (clear focus)
u8   ttl            seconds, default 60 ; the receiver re-sends Focus every 5 s while active
u8   chip_px        0 = 32, 1 = 64, 2 = 96 pixels square ; bit7 colour
```

At most 4 contacts focused at once; a fifth replaces the oldest. Focus expires at `ttl` without
renewal, so a dead uplink cannot leave the edge stuck in a high-rate mode.

### 4.3 `Clock` (type 0x83, body 4 B)

`u32 utc` seconds. For edges without a clock (no GNSS); the edge fills `utc_at_tick0` from it.

### 4.4 `ChipAck` (type 0x84, body 3 B)

`u16 contact ; u8 chip` : decoded, stop sending symbols.

## 5. Receiver behaviour

### 5.1 Merge rule

Per `(session, id)`: accept a record if `rev` is newer (unsigned wrap: `(rev - held) as i8 > 0`),
or equal `rev` with a newer frame `tick` (same state, fresher `age`). Older records are dropped
silently; they are expected (repeats and reordering). A `departed` tombstone wins over any
non-tombstone of the same id regardless of `rev` and is kept for `2 x T_floor` so a late repeat of
a live record cannot resurrect it.

### 5.2 Derived events (never on the wire)

| event | when the merge changes | event time |
|---|---|---|
| new contact | first record of an id (not a tombstone) | `first_seen` |
| confirmed | `confirmed` goes 0 -> 1 | frame tick - age |
| started moving / stopped / static | `motion` changes | `since` |
| grew / shrank | `count` changes | frame tick - age |
| lost | `lost` goes 0 -> 1 | frame tick - age |
| departed | tombstone arrives | frame tick - age |
| split / merged | children appear, or a child's `parent` changes | frame tick - age |

Event time comes from the record, not from arrival, so a receiver that was in a blackout gets the
right timeline when the link returns. Delta's "first detected at" is `first_seen` + `utc_at_tick0`.

### 5.3 Dead reckoning and the error radius

A moving contact (flags motion = 2, has velocity) is extrapolated along `course` at `speed`,
capped by the class prior's max speed (core `classes.rs`), with the same deterministic predictor
both ends share. The shown error radius is

`ce_shown = ce + (moving ? speed : 0) x silence + (static ? 0 : class_max_speed x max(0, silence - T_ladder_last))`

where `silence` is the time since the record's observation (`frame tick - age`). In words: a
moving contact's circle grows with its speed at once; any non-static contact's circle also grows at
the class cap once it is overdue on the ladder (the edge would have told us it moved). A static
contact's circle does not grow: nothing moved, or the edge would have revised it.

### 5.4 Liveness

| state | condition |
|---|---|
| fresh | last record younger than 3 x its expected interval (ladder step or `T_floor`) |
| unheard | older than that: shown grey ; it is the *link* that is silent, the edge may still see it |
| lost | the edge said so (flags.bit3): last known position, dashed |
| departed | tombstone: removed from the live picture, kept in the log |
| incomplete picture | fewer live contacts held than `Ego.n_contacts` |

Device silence: no frame at all for `3 x T_ego` -> the whole device is *unheard*; every contact's
circle grows per 5.3; `Ego`'s last `nav` is shown with its age ("RTH, 42 s ago").

## 6. Edge scheduling

All times scale with the budget through `f = clamp(800 / budget_bps, 0.125, 8)` (f = 1 at
100 B/s; the unlimited budget 0 counts as f = 0.125). The edge computes these itself from the
budget it last heard (or its configured default); the receiver computes the same from the budget
it last advertised (as v1's cadence).

| name | value | what |
|---|---|---|
| `T_ladder` | 0, 2f, 6f, 14f, 30f s after a change, then `T_floor` | repeats of a changed record: 3 copies in 6f s survive 70 % random loss; the spread covers jamming bursts |
| `T_floor` | 60f s (min 10 s) | repeat interval of an unchanged or acked record ("once in several frames") |
| `T_focus` | max(1 s, 1f) | focused contacts, constant, no doubling |
| `T_ego` | max(1 s, 5f) | `Ego` |
| `T_session` | max(5 s, 30f) | `Session` |
| `T_lost` | 5 s | no look -> lost |
| `T_depart` | 60 s lost -> tombstone | tombstone then rides the ladder and stays 2 x `T_floor` |
| `T_stopped` | 30 s | stopped -> static |

Each record has a *due time*. Every time the frame builder runs (whenever the link can take a
frame of the target size), it fills the frame with the most overdue records first, in this order
of class when ties: focused, `Ego` if due, changed contacts on the ladder, `Session` if due,
floor repeats, tombstones, `Note`, chip symbols. A record that does not fit waits; the builder
never splits a record. Chip symbols take the remaining bytes only, and never more than half the
budget over any 10 s window.

A contact's `rev` increments (and its ladder restarts) when any of these happens:

- motion state changes (static <-> moving <-> stopped), `lost`, `departed`, `confirmed`;
- its centroid deviates from the receiver's dead reckoning of the last *sent* record by more than
  `max(ce, 2 x pos_res)` (half that under focus);
- `count` changes, or the class mix changes;
- `ce` changes by more than 50 % (GNSS lost or regained changes every contact's ce at once: the
  edge then spreads the revisions over one `T_ego` instead of bursting);
- course changes by more than 30° or speed by more than 25 % while moving.

Nothing else moves `rev`: a static contact in a stable scene is sent at `T_floor` only.

Budget: as v1, bit/s on the link including the carrier's per-frame overhead (28 B on UDP/IP, 0 on
serial, 12 B on MAVLink tunnel). The edge measures what it emits over a 10 s window and, when over
budget, stretches `f` (never below the ladder's first repeat) before dropping anything. Chips are
the first thing starved, floor repeats the second, `Ego` and changed contacts never.

## 7. Sizes

| item | bytes on the wire (TLV included) |
|---|---|
| frame header | 10 (12 with CRC) |
| `Session` | 26 |
| `Ego` | 28 |
| `Contact`, static single | 23 |
| `Contact`, moving group | 25 |
| `Contact`, child under focus, moving | 27 |
| `ChipSym`, S = 32 | 38 |
| `Digest`, 8 contacts | 31 |
| `Focus` | 7 |

Worked floor at 100 B/s (f = 1), 20 static contacts: contacts 20 x 23 B / 60 s = 7.7 B/s, `Ego`
28 B / 5 s = 5.6 B/s, `Session` 26 B / 30 s = 0.9 B/s, frame headers about 12 x 10 B / 60 s =
2 B/s: **about 16 B/s, 16 % of the link.** The remaining 84 B/s carries about 3.5 contact
revisions per second, or one focused contact at 1 Hz plus two revisions per second. At 12.5 B/s
(ELRS 1:128, f = 8) the same scene floors at 2 B/s with `T_floor` = 8 min and `Ego` every 40 s:
usable for "what is there", not for following movement.

## 8. Durability, stated as guarantees

Let `B` be the length of a blackout (no frame delivered either way).

1. **Any frame is useful.** Every record in a delivered frame is applied; nothing waits for a
   repair round trip. One `Session` record (every `T_session`) is the only prerequisite for lat/lon.
2. **State converges without an uplink** within `T_floor` after the link returns, whatever `B`:
   every live record is re-sent at least once per `T_floor`. With an uplink, within one `Digest`
   round trip plus the time of the missing records.
3. **Events survive** if the contact is still live or its tombstone still rides (`B` < `T_depart`
   + 2 x `T_floor`, i.e. 3 min at f = 1), because the event times are inside the state.
4. **Limit, said plainly:** a contact that appeared *and* departed during a blackout longer than
   its tombstone life is lost from the live stream. The edge keeps a full event log on its own
   storage (`docs/DESIGN_V2.md` §7) for upload over any later link; the stream is for now, the log
   for afterwards.
5. **Random loss** `p` delays a change by at most the first ladder step that gets through: with
   copies at 0, 2f, 6f, the chance a change is still unknown after 6f s is `p^3` (0.1 % at 10 %
   loss, 12.5 % at 50 %).
6. **Reordering and duplication** are no-ops by 5.1.

## 9. Peeking and logging

Every frame is describable without session state: `peek(bytes)` returns `{session, seq, tick,
uplink, records:[{type, len}]}`. `describe(bytes)` adds one line per record. Both exist in Rust
and are exported to WASM and Swift as in v1.

## 10. Mapping to consumers

| field | CoT (ATAK, TAK server, Delta via ATAK) | STANAG 4607-like target report |
|---|---|---|
| `pos` + `Session.origin` | `point.lat/lon` | delta lat/lon from the dwell centre |
| `ce` (grown per 5.3) | `point.ce`; `le` = ce when alt known else 9999999 | slant/cross range uncertainty |
| `course`, `speed` | `detail/track course speed` | velocity line of sight (not directly) |
| class mix | callsign and remarks ("3 vehicles, 1 armoured"), type stays `a-u-G` | classification + class probability |
| `conf` | remarks | SNR / class probability |
| `first_seen`, `since` | `time` = first_seen, `start` = since | dwell time |
| `how` | `m-p` single edge, `m-f` fused from >= 2 edges, `h-g-i-g-o` when `ext.bit4` | - |
| `Ego` | a `a-f-A-M-F-Q` (friendly UAV) event with `hae`, track course/speed, battery in remarks; footprint as a `u-d-c` circle | dwell segment: sensor position, orientation, area |
| `stale` | `unheard`/`lost` -> `stale = now` | - |
