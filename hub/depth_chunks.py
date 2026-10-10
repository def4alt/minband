"""Depth-keyframe chunk wire format, frozen in proto/DEPTH.md.

Mirrors edge/edge.py's `encode_depth_chunk`/`decode_depth_chunk` byte-for-byte (same header
layout, same QUAT_SCALE/POS_SCALE) -- duplicated here rather than imported because edge/edge.py
imports clika_runtime and cv2 at module scope, dependencies this process (recon.py, no detection
or depth model of its own) has no business dragging in. This module only needs stdlib + numpy,
same as shared/geom.py. The frozen struct layout is the single source of truth (proto/DEPTH.md);
this is an independent reader of it, not a second implementation of an algorithm.
"""
import struct

import numpy as np

QUAT_SCALE = 32767.0
POS_SCALE = 10.0  # decimetres -> metres
_HEADER = struct.Struct("<BBHBBBBB")  # kind,drone_id,kf_id,row0,nrows,w,h,flags = 9 B
MAX_CHUNK_BYTES = 200  # hard ceiling (proto/DEPTH.md) -- payloads over ~1000 B lose 88-94% over BLE


def encode_depth_chunk(drone_id, kf_id, row0, rows_u8, quat_wlast, pos_m, dmin, dmax, h=48):
    """Test/dev-only encoder (edge.py is the real producer) -- used by test_recon.py to build a
    synthetic chunk stream without depending on edge/edge.py's heavy imports. `h` is the full
    keyframe height (always 48 at the chosen operating point), not this chunk's own row count."""
    nrows, w = rows_u8.shape
    flags = 0b01  # bit0 has_position=1, bit1 metric_depth=0 (relative depth)
    header = _HEADER.pack(0x01, drone_id, kf_id, row0, nrows, w, h, flags)
    q = np.clip(np.round(np.asarray(quat_wlast, dtype=np.float64) * QUAT_SCALE), -32767, 32767).astype("<i2")
    pos = np.clip(np.round(np.asarray(pos_m, dtype=np.float64) * POS_SCALE), -32767, 32767).astype("<i2")
    body = (header + q.tobytes() + pos.tobytes()
            + np.float16(dmin).tobytes() + np.float16(dmax).tobytes()
            + np.ascontiguousarray(rows_u8, dtype=np.uint8).tobytes())
    assert len(body) <= MAX_CHUNK_BYTES, f"chunk {len(body)} B exceeds {MAX_CHUNK_BYTES} B ceiling"
    return body


def decode_depth_chunk(buf):
    """-> dict(kind, drone_id, kf_id, row0, nrows, w, h, flags, quat, pos, dmin, dmax, rows)
    where `quat` is (x,y,z,w) float, `pos` is (x,y,z) metres, `rows` is uint8 (nrows, w)."""
    kind, drone_id, kf_id, row0, nrows, w, h, flags = _HEADER.unpack(buf[:9])
    off = 9
    quat = np.frombuffer(buf, dtype="<i2", count=4, offset=off) / QUAT_SCALE; off += 8
    pos = np.frombuffer(buf, dtype="<i2", count=3, offset=off) / POS_SCALE; off += 6
    dmin = float(np.frombuffer(buf, dtype="<f2", count=1, offset=off)[0]); off += 2
    dmax = float(np.frombuffer(buf, dtype="<f2", count=1, offset=off)[0]); off += 2
    rows = np.frombuffer(buf, dtype=np.uint8, count=nrows * w, offset=off).reshape(nrows, w)
    return {
        "kind": kind, "drone_id": drone_id, "kf_id": kf_id, "row0": row0, "nrows": nrows,
        "w": w, "h": h, "flags": flags, "quat": quat, "pos": pos, "dmin": dmin, "dmax": dmax,
        "rows": rows,
    }


def dequantize(chunk):
    """chunk's quantized 0-255 rows -> the model's original relative-depth values, per
    proto/DEPTH.md's `(value - dmin) / (dmax - dmin) * 255` quantization, inverted."""
    dmin, dmax = chunk["dmin"], chunk["dmax"]
    if dmax <= dmin:
        return np.zeros(chunk["rows"].shape, dtype=np.float32)
    return dmin + (chunk["rows"].astype(np.float32) / 255.0) * (dmax - dmin)


def _demo():
    rows = np.array([[10, 20, 30], [40, 50, 60]], dtype=np.uint8)
    buf = encode_depth_chunk(drone_id=1, kf_id=7, row0=4, rows_u8=rows,
                              quat_wlast=(0.0, 0.0, 0.0, 1.0), pos_m=(1.5, 2.0, -3.25),
                              dmin=0.1, dmax=9.9)
    assert len(buf) <= MAX_CHUNK_BYTES
    d = decode_depth_chunk(buf)
    assert d["drone_id"] == 1 and d["kf_id"] == 7 and d["row0"] == 4
    assert d["nrows"] == 2 and d["w"] == 3
    assert np.array_equal(d["rows"], rows)
    assert abs(d["pos"][2] - (-3.25)) < 0.1  # decimetre quantization: 0.1 m resolution
    assert abs(d["quat"][3] - 1.0) < 1e-3
    assert abs(d["dmin"] - 0.1) < 1e-2 and abs(d["dmax"] - 9.9) < 1e-2
    deq = dequantize(d)
    assert deq.shape == rows.shape
    print("hub/depth_chunks.py: ALL OK")


if __name__ == "__main__":
    _demo()
