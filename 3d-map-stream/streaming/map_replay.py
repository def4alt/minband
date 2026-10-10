#!/usr/bin/env python
"""Replay a recorded live map stream (map_sender_slot.py --record FILE) into a receiver with the SENDER's timing,
i.e. what the operator would see over a link with no jitter (only the configured --rate pacing, which is already in the
recorded send times).  Frames: <d t_send><B kind><I len> payload; on the wire as the sender: <I len><payload>.
Usage: python map_replay.py stream_record.bin --connect 127.0.0.1:PORT [--speed 1] [--latency 0]"""
import argparse, socket, struct, time, sys

def frames(path):
    with open(path, "rb") as f:
        while True:
            h = f.read(13)
            if len(h) < 13: return
            t, kind, n = struct.unpack("<dBI", h); p = f.read(n)
            if len(p) < n: return
            yield t, kind, p

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("record"); ap.add_argument("--connect", default="127.0.0.1:5555")
    ap.add_argument("--speed", type=float, default=1.0); ap.add_argument("--latency", type=float, default=0.0, help="constant one-way delay to add (s)")
    ap.add_argument("--wait", type=float, default=60.0, help="s to wait for the receiver")
    a = ap.parse_args(); fr = list(frames(a.record))
    if not fr: sys.exit("empty record")
    host, port = a.connect.rsplit(":", 1); t_w = time.time()
    while True:
        try: s = socket.create_connection((host, int(port)), timeout=5); break
        except OSError:
            if time.time() - t_w > a.wait: sys.exit(f"no receiver at {a.connect}")
            time.sleep(0.3)
    s.settimeout(3)
    try: s.recv(4)  # receiver greeting b'MAPR'
    except socket.timeout: pass
    s.settimeout(None); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    t_first = fr[0][0]; t0 = time.time() + a.latency; nb = 0; kinds = {}
    print(f"[replay] {len(fr)} frames over {fr[-1][0] - t_first:.1f} s (speed {a.speed:g}, +{a.latency:g} s latency)", flush=True)
    for t, kind, p in fr:
        due = t0 + (t - t_first) / a.speed; d = due - time.time()
        if d > 0: time.sleep(d)
        s.sendall(struct.pack("<I", len(p)) + p); nb += len(p) + 4; kinds[kind] = kinds.get(kind, 0) + 1
    s.close(); print(f"[replay] done: {nb} B, frames by kind (0 map, 1 kf pose, 2 live pose, 3 status) {kinds}", flush=True)

if __name__ == "__main__": main()
