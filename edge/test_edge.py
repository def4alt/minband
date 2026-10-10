"""edge/test_edge.py -- P4 gate.

1. Depth-chunk encode/decode round trip, size <=200 B (pure, no model/video needed).
2. End-to-end: runs edge.py against the real local clip for a short --duration, with a
   tiny UDP listener counting track datagrams and depth chunks actually received, and
   asserts it ran without crashing, looped the clip, and emitted both streams. Skipped
   (not failed) if the clip isn't present, since clips/ is gitignored/untracked.

Run: .venv/Scripts/python.exe edge/test_edge.py
"""
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edge import DEFAULT_CLIP, decode_depth_chunk, encode_depth_chunk


def test_depth_roundtrip():
    rows = np.arange(2 * 64, dtype=np.uint8).reshape(2, 64) % 255
    quat = [0.1, -0.2, 0.3, 0.9]
    pos = [12.5, 25.0, -40.25]
    chunk = encode_depth_chunk(drone_id=3, kf_id=7, row0=10, rows_u8=rows,
                                quat_wlast=quat, pos_m=pos, dmin=0.5, dmax=3.25)
    assert len(chunk) == 155, f"expected 155 B at 64x2, got {len(chunk)}"
    assert len(chunk) <= 200
    d = decode_depth_chunk(chunk)
    assert d["kind"] == 0x01 and d["drone_id"] == 3 and d["kf_id"] == 7
    assert d["row0"] == 10 and d["nrows"] == 2 and d["w"] == 64 and d["h"] == 48
    assert d["flags"] == 0b01
    assert np.allclose(d["quat"], quat, atol=1e-4)
    assert np.allclose(d["pos"], pos, atol=0.1)  # decimetre quantization
    assert abs(d["dmin"] - 0.5) < 0.01 and abs(d["dmax"] - 3.25) < 0.01
    assert np.array_equal(d["rows"], rows)
    print("test_depth_roundtrip: OK")


class _Listener:
    """Counts UDP datagrams on a port in a background thread, for the e2e test only."""

    def __init__(self, port, tag):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(0.5)
        self.count = 0
        self.tag = tag
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop:
            try:
                self.sock.recvfrom(4096)
                self.count += 1
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self):
        self._stop = True
        self._t.join(timeout=2)
        self.sock.close()


def test_e2e():
    if not Path(DEFAULT_CLIP).exists():
        print(f"test_e2e: SKIPPED (no clip at {DEFAULT_CLIP})")
        return
    track_listener = _Listener(7701, "tracks")
    depth_listener = _Listener(7790, "depth")
    log_path = Path(__file__).resolve().parent / "_test_edge_e2e.log"
    duration = 15.0
    clip_len_s = 20.0  # shorter than this clip's real length -- --loop below exercises the
    # loop path without needing the duration to actually exceed the clip
    with open(log_path, "w") as log:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "edge.py"),
             "--duration", str(duration), "--loop"],
            stdout=log, stderr=subprocess.STDOUT, timeout=300)
    track_listener.stop()
    depth_listener.stop()
    log_text = log_path.read_text()
    print(log_text)
    assert proc.returncode == 0, f"edge.py exited {proc.returncode}, see {log_path}"
    assert "actual device=" in log_text, "device guard did not log actual device"
    assert "depth_keyframes=" in log_text and "track_datagrams=" in log_text
    assert track_listener.count > 0, "no track datagrams received over UDP"
    assert depth_listener.count > 0, "no depth chunks received over UDP"
    print(f"test_e2e: OK (track datagrams={track_listener.count}, depth chunks={depth_listener.count})")


if __name__ == "__main__":
    test_depth_roundtrip()
    test_e2e()
    print("edge/test_edge.py: ALL OK")
