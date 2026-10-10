# Depth keyframe chunk wire format (frozen, P4)

Produced by `edge/edge.py` (`encode_depth_chunk`/`decode_depth_chunk`), sent as individual
UDP datagrams to `127.0.0.1:<--depth-port>` (default 7790). Each datagram is one
self-contained chunk -- no reassembly needed to read a single chunk's rows, but a full
depth map needs all chunks for one `kf_id` collected.

Target resolution: depth map resized to **64 x 48** before chunking, zeroed over any
current detection box region (so the background/obstacle map isn't polluted by moving
objects). Chunked by row pairs (`ROWS_PER_CHUNK = 2`): `ceil(48 / 2) = 24` chunks per
keyframe.

## Layout (little-endian), 9-byte header + 18-byte pose block + payload

| field | type | bytes | notes |
|---|---|---|---|
| `kind` | u8 | 1 | `0x01` |
| `drone_id` | u8 | 1 | |
| `kf_id` | u16 | 2 | increments per full keyframe, **not** per chunk |
| `row0` | u8 | 1 | first row this chunk covers (0, 2, 4, ... 46) |
| `nrows` | u8 | 1 | rows in this chunk (2, except a final short chunk if `h` isn't a multiple of 2) |
| `w` | u8 | 1 | 64 |
| `h` | u8 | 1 | 48 (full map height, not this chunk's) |
| `flags` | u8 | 1 | bit0 `has_position` (always 1 here -- static adapter always has `p`); bit1 `metric_depth` (always 0 -- Depth-Anything-V2 output is relative, not metres) |
| `q[4]` | i16 x4 | 8 | w-last quaternion `(x,y,z,w)`, camera-to-world, each component scaled by `QUAT_SCALE = 32767` and clamped to int16 range |
| `pos[3]` | i16 x3 | 6 | camera position, world frame, decimetres (`metres * 10`), clamped to int16 range (+-3276.7 m) |
| `dmin` | f16 | 2 | numpy.float16, min of the 64x48 map this keyframe |
| `dmax` | f16 | 2 | numpy.float16, max of the 64x48 map this keyframe |
| rows | u8 x (`nrows*w`) | up to 128 | depth quantized to 0-255 via `(value - dmin) / (dmax - dmin) * 255`, row-major |

Header + pose block = 27 B. At the chosen 2-row/64-wide chunk size: **155 B total**,
comfortably under the **200 B hard ceiling** (payloads over ~1000 B lose 88-94% over the
real BLE link -- this is a measured constraint, not a guess).

## Emission triggers (`DepthKeyframer` in `edge/edge.py`)

A new keyframe (all 24 chunks, re-chunked and re-sent) is emitted when: it's the first
depth tick, OR 10 s have elapsed since the last keyframe (`max_age_s`), OR the mean
absolute per-pixel depth change since the last keyframe exceeds 8% of that keyframe's own
dmin-dmax range (`change_frac`). The plan's third trigger -- a new yaw/position bin -- is
not implemented: `shared/pose_source.py`'s `static_adapter` never changes pose, so that
trigger is dead code against the only pose source this phase runs against; the 10 s
max-age trigger is what actually keeps keyframes flowing on a static camera.

## Quaternion/position scale rationale

`core/src/wire.rs`'s existing wire messages (`Message::Pose`) send `quat`/`pos` as raw
`f32`, not a quantized int type -- there's no existing `theta_q`-style int16 quaternion
scale to match. `QUAT_SCALE = 32767` / `POS_SCALE = 10` (decimetres) are simple, documented
choices for *this* depth-chunk format only; not claimed compatible with any other MinBand
wire message.
