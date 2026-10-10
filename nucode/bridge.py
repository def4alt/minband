#!/usr/bin/env python3
"""UDP <-> SLIP/CRC serial bridge for the MinBand NUCODE BLE link.

Usage:
    python bridge.py --serial COM7 [--baud 1000000]
        [--udp-listen 7788] [--udp-forward 127.0.0.1:7777]
        [--http-port 8765] [--quiet]
    python bridge.py --selftest

Two asymmetric roles, selected by which UDP flags you pass:
  - Bridge A (edge side): --udp-listen only. The sim sends datagrams from
    an ephemeral port; acks arriving from serial go back to the last peer
    address seen on the socket (NOT to a fixed forward address, since the
    sim's source port is ephemeral and unknown ahead of time).
  - Bridge B (hub side): --udp-forward only. Datagrams from serial go to
    the fixed forward address (the real server); its acks come back on the
    same ephemeral local socket and get re-framed onto serial.

Rule: serial->UDP destination = --udp-forward if given, else the last peer
seen on the socket; if neither is available yet, drop (and count) the frame.
"""
import argparse
import http.server
import json
import socket
import struct
import sys
import threading
import time

import serial

SLIP_END = 0xC0
SLIP_ESC = 0xDB
SLIP_ESC_END = 0xDC
SLIP_ESC_ESC = 0xDD

TYPE_DATA = 0x01
TYPE_TELEMETRY = 0x02

MAX_PAYLOAD = 1200


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


def slip_encode(frame_type: int, payload: bytes, seq: int = 0) -> bytes:
    # TYPE_DATA carries a monotonic u16 sequence number right after the type
    # byte, so the receiver can detect gaps (lost frames) even when a frame
    # vanishes cleanly with no corrupt bytes left behind to trip the CRC
    # check. No ack/retry - just loss visibility for a lossy link.
    if frame_type == TYPE_DATA:
        body = bytes([frame_type]) + struct.pack("<H", seq) + payload
    else:
        body = bytes([frame_type]) + payload
    crc = crc16_ccitt(body)
    body += bytes([crc & 0xFF, (crc >> 8) & 0xFF])

    out = bytearray([SLIP_END])
    for b in body:
        if b == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


class SlipDecodeError(Exception):
    pass


def slip_decode(raw: bytes) -> tuple[int, int | None, bytes]:
    """Decode one de-escaped frame's body (no END bytes). Returns (type, seq, payload);
    seq is None for non-TYPE_DATA frames. Raises SlipDecodeError on bad CRC,
    too-short frame, or unknown type."""
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == SLIP_ESC:
            i += 1
            if i >= len(raw):
                raise SlipDecodeError("truncated escape")
            nxt = raw[i]
            if nxt == SLIP_ESC_END:
                out.append(SLIP_END)
            elif nxt == SLIP_ESC_ESC:
                out.append(SLIP_ESC)
            else:
                raise SlipDecodeError("bad escape sequence")
        else:
            out.append(b)
        i += 1

    if len(out) < 3:
        raise SlipDecodeError("frame too short")

    frame_type = out[0]
    if frame_type == TYPE_DATA:
        if len(out) < 5:
            raise SlipDecodeError("frame too short")
        seq = out[1] | (out[2] << 8)
        payload = bytes(out[3:-2])
    else:
        seq = None
        payload = bytes(out[1:-2])
    crc_received = out[-2] | (out[-1] << 8)
    crc_computed = crc16_ccitt(bytes(out[:-2]))
    if crc_received != crc_computed:
        raise SlipDecodeError("bad CRC")
    if frame_type not in (TYPE_DATA, TYPE_TELEMETRY):
        raise SlipDecodeError("unknown type")
    if len(payload) > MAX_PAYLOAD:
        raise SlipDecodeError("oversize payload")

    return frame_type, seq, payload


class SlipFramer:
    """Accumulates raw serial bytes, yields (type, payload) per complete frame."""

    def __init__(self):
        self._buf = bytearray()
        self._in_frame = False

    def feed(self, data: bytes):
        frames = []
        for b in data:
            if b == SLIP_END:
                if self._in_frame and self._buf:
                    frames.append(bytes(self._buf))
                self._buf = bytearray()
                self._in_frame = True
            elif self._in_frame:
                self._buf.append(b)
        return frames


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.serial_to_udp_bytes = 0
        self.serial_to_udp_frames = 0
        self.udp_to_serial_bytes = 0
        self.udp_to_serial_frames = 0
        self.crc_errors = 0
        self.oversize_drops = 0
        self.unknown_type_drops = 0
        self.seq_lost = 0
        self.seq_last = None
        self.last_telemetry = None

    def snapshot_and_reset_rates(self):
        with self.lock:
            s = dict(
                serial_to_udp_bytes=self.serial_to_udp_bytes,
                serial_to_udp_frames=self.serial_to_udp_frames,
                udp_to_serial_bytes=self.udp_to_serial_bytes,
                udp_to_serial_frames=self.udp_to_serial_frames,
                crc_errors=self.crc_errors,
                oversize_drops=self.oversize_drops,
                unknown_type_drops=self.unknown_type_drops,
                seq_lost=self.seq_lost,
                seq_last=self.seq_last,
            )
            self.serial_to_udp_bytes = 0
            self.serial_to_udp_frames = 0
            self.udp_to_serial_bytes = 0
            self.udp_to_serial_frames = 0
            return s


class Bridge:
    def __init__(self, ser: serial.Serial, udp_sock: socket.socket,
                 udp_forward, quiet: bool):
        self.ser = ser
        self.udp_sock = udp_sock
        self.udp_forward = udp_forward  # (host, port) or None
        self.last_peer = None
        self.quiet = quiet
        self.stats = Stats()
        self.serial_write_lock = threading.Lock()
        self.cumulative = Stats()
        self.tx_seq = 0
        self.rx_expected_seq = None

    def write_frame(self, frame_type: int, payload: bytes):
        with self.serial_write_lock:
            seq = self.tx_seq
            if frame_type == TYPE_DATA:
                self.tx_seq = (self.tx_seq + 1) & 0xFFFF
            self.ser.write(slip_encode(frame_type, payload, seq))

    def serial_reader_loop(self):
        framer = SlipFramer()
        while True:
            try:
                n = self.ser.in_waiting
                data = self.ser.read(n if n > 0 else 1)
            except serial.SerialException:
                return
            if not data:
                continue
            for raw in framer.feed(data):
                try:
                    frame_type, seq, payload = slip_decode(raw)
                except SlipDecodeError as e:
                    with self.stats.lock:
                        if "CRC" in str(e):
                            self.stats.crc_errors += 1
                        elif "oversize" in str(e):
                            self.stats.oversize_drops += 1
                        else:
                            self.stats.unknown_type_drops += 1
                    continue

                if frame_type == TYPE_DATA:
                    with self.stats.lock:
                        if self.rx_expected_seq is not None:
                            self.stats.seq_lost += (seq - self.rx_expected_seq) & 0xFFFF
                        self.rx_expected_seq = (seq + 1) & 0xFFFF
                        self.stats.seq_last = seq
                    dest = self.udp_forward or self.last_peer
                    if dest is None:
                        continue  # no destination known yet; drop
                    self.udp_sock.sendto(payload, dest)
                    with self.stats.lock:
                        self.stats.serial_to_udp_bytes += len(payload)
                        self.stats.serial_to_udp_frames += 1
                elif frame_type == TYPE_TELEMETRY:
                    try:
                        telemetry = json.loads(payload.decode("utf-8"))
                    except Exception:
                        telemetry = {"raw_hex": payload.hex()}
                    with self.stats.lock:
                        self.stats.last_telemetry = telemetry

    def udp_reader_loop(self):
        while True:
            try:
                data, addr = self.udp_sock.recvfrom(2048)
            except OSError:
                return
            self.last_peer = addr
            if len(data) > MAX_PAYLOAD:
                with self.stats.lock:
                    self.stats.oversize_drops += 1
                continue
            self.write_frame(TYPE_DATA, data)
            with self.stats.lock:
                self.stats.udp_to_serial_bytes += len(data)
                self.stats.udp_to_serial_frames += 1

    def stats_ticker_loop(self):
        while True:
            time.sleep(1)
            rates = self.stats.snapshot_and_reset_rates()
            with self.cumulative.lock:
                self.cumulative.serial_to_udp_bytes += rates["serial_to_udp_bytes"]
                self.cumulative.serial_to_udp_frames += rates["serial_to_udp_frames"]
                self.cumulative.udp_to_serial_bytes += rates["udp_to_serial_bytes"]
                self.cumulative.udp_to_serial_frames += rates["udp_to_serial_frames"]
            if not self.quiet:
                with self.stats.lock:
                    telem = self.stats.last_telemetry
                print(
                    f"[1Hz] serial->udp {rates['serial_to_udp_bytes']}B/s "
                    f"{rates['serial_to_udp_frames']}f/s | "
                    f"udp->serial {rates['udp_to_serial_bytes']}B/s "
                    f"{rates['udp_to_serial_frames']}f/s | "
                    f"crc_err={rates['crc_errors']} oversize={rates['oversize_drops']} "
                    f"unknown_type={rates['unknown_type_drops']} seq_lost={rates['seq_lost']} "
                    f"telemetry={telem}"
                )

    def make_http_handler(self):
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                if self.path != "/telemetry":
                    self.send_response(404)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    return
                with bridge.stats.lock:
                    payload = {
                        "telemetry": bridge.stats.last_telemetry,
                        "crc_errors": bridge.stats.crc_errors,
                        "oversize_drops": bridge.stats.oversize_drops,
                        "unknown_type_drops": bridge.stats.unknown_type_drops,
                        "seq_lost": bridge.stats.seq_lost,
                        "seq_last": bridge.stats.seq_last,
                    }
                with bridge.cumulative.lock:
                    payload["cumulative_serial_to_udp_frames"] = bridge.cumulative.serial_to_udp_frames
                    payload["cumulative_udp_to_serial_frames"] = bridge.cumulative.udp_to_serial_frames
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def parse_host_port(s: str):
    host, port = s.rsplit(":", 1)
    return (host, int(port))


def run_selftest() -> int:
    failures = []

    # SLIP round-trip with all special bytes present.
    payload = bytes([SLIP_END, SLIP_ESC, 0x00, SLIP_ESC, SLIP_ESC_END, 0xFF]) + bytes(range(250))
    encoded = slip_encode(TYPE_DATA, payload)
    assert encoded[0] == SLIP_END and encoded[-1] == SLIP_END
    inner = encoded[1:-1]
    framer = SlipFramer()
    frames = framer.feed(bytes([SLIP_END]) + inner + bytes([SLIP_END]))
    if len(frames) != 1:
        failures.append(f"SLIP round-trip: expected 1 frame, got {len(frames)}")
    else:
        t, seq, p = slip_decode(frames[0])
        if t != TYPE_DATA or p != payload:
            failures.append("SLIP round-trip: payload mismatch")

    # Max 1200 B payload round-trips.
    big_payload = bytes((i % 256 for i in range(MAX_PAYLOAD)))
    encoded_big = slip_encode(TYPE_DATA, big_payload)
    frames = SlipFramer().feed(encoded_big)
    if len(frames) != 1:
        failures.append(f"1200B round-trip: expected 1 frame, got {len(frames)}")
    else:
        t, seq, p = slip_decode(frames[0])
        if p != big_payload:
            failures.append("1200B round-trip: payload mismatch")

    # Known CRC16-CCITT-FALSE test vector.
    crc = crc16_ccitt(b"123456789")
    if crc != 0x29B1:
        failures.append(f"CRC test vector: expected 0x29B1, got {hex(crc)}")

    # Corrupted CRC must be detected.
    corrupted = bytearray(SlipFramer().feed(slip_encode(TYPE_DATA, b"hello"))[0])
    corrupted[-1] ^= 0xFF
    try:
        slip_decode(bytes(corrupted))
        failures.append("bad CRC: expected SlipDecodeError, none raised")
    except SlipDecodeError as e:
        if "CRC" not in str(e):
            failures.append(f"bad CRC: wrong error type: {e}")

    # Resync after noise mid-stream: noise bytes before a valid frame must
    # not corrupt it (framer only starts collecting after it sees END).
    noise = bytes([0x41, 0x42, 0xFF, 0x00])
    valid_frame_bytes = slip_encode(TYPE_DATA, b"resync-ok")
    frames = SlipFramer().feed(noise + valid_frame_bytes)
    if len(frames) != 1:
        failures.append(f"resync after noise: expected 1 frame, got {len(frames)}")
    else:
        t, seq, p = slip_decode(frames[0])
        if p != b"resync-ok":
            failures.append("resync after noise: payload mismatch")

    # Telemetry (type 0x02) decode.
    telem_payload = json.dumps({"tx_power": 4, "rssi": -55}).encode("utf-8")
    encoded_telem = slip_encode(TYPE_TELEMETRY, telem_payload)
    frames = SlipFramer().feed(encoded_telem)
    if len(frames) != 1:
        failures.append(f"telemetry decode: expected 1 frame, got {len(frames)}")
    else:
        t, seq, p = slip_decode(frames[0])
        if t != TYPE_TELEMETRY:
            failures.append("telemetry decode: wrong type")
        decoded = json.loads(p.decode("utf-8"))
        if decoded.get("tx_power") != 4 or decoded.get("rssi") != -55:
            failures.append("telemetry decode: field mismatch")

    # TYPE_DATA sequence number round-trips and the gap-detection math
    # (used to count cleanly-vanished frames with no corrupt bytes left
    # behind) handles u16 wraparound correctly.
    for seq_in in (0, 1234, 65535):
        frames = SlipFramer().feed(slip_encode(TYPE_DATA, b"x", seq=seq_in))
        _, seq_out, _ = slip_decode(frames[0])
        if seq_out != seq_in:
            failures.append(f"seq round-trip: expected {seq_in}, got {seq_out}")
    gap_cases = [
        (6, 6, 0),       # next expected arrives in order: no gap
        (6, 9, 3),       # skipped 6,7,8: gap of 3
        (0, 0, 0),       # wraps around, in order: no gap
        (65535, 1, 2),   # skipped 65535,0: gap of 2
    ]
    for expected_seq, received_seq, want_gap in gap_cases:
        gap = (received_seq - expected_seq) & 0xFFFF
        if gap != want_gap:
            failures.append(
                f"seq gap math: expected={expected_seq} received={received_seq} "
                f"want gap={want_gap}, got {gap}"
            )

    if failures:
        for f in failures:
            print("FAIL:", f, file=sys.stderr)
        return 1
    print("selftest OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial")
    ap.add_argument("--baud", type=int, default=1000000)
    ap.add_argument("--udp-listen", type=int)
    ap.add_argument("--udp-forward")
    ap.add_argument("--http-port", type=int)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return run_selftest()

    if not args.serial:
        ap.error("--serial is required (unless --selftest)")

    ser = serial.Serial(args.serial, args.baud, timeout=0.1)

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.udp_listen:
        udp_sock.bind(("0.0.0.0", args.udp_listen))
    else:
        udp_sock.bind(("0.0.0.0", 0))

    udp_forward = parse_host_port(args.udp_forward) if args.udp_forward else None

    bridge = Bridge(ser, udp_sock, udp_forward, args.quiet)

    threads = [
        threading.Thread(target=bridge.serial_reader_loop, daemon=True),
        threading.Thread(target=bridge.udp_reader_loop, daemon=True),
        threading.Thread(target=bridge.stats_ticker_loop, daemon=True),
    ]
    for t in threads:
        t.start()

    if args.http_port:
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", args.http_port), bridge.make_http_handler())
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
