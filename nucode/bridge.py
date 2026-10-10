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
import collections
import http.server
import json
import random
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
TYPE_DEPTH = 0x03

MAX_PAYLOAD = 1200

# Outbound priority scheduler: A (MinBand data) beats B (telemetry) beats
# C (depth). C is still guaranteed a floor share of bandwidth so a busy
# data channel can never fully starve the depth/map channel.
PRIO_A, PRIO_B, PRIO_C = 0, 1, 2
TYPE_TO_PRIORITY = {TYPE_DATA: PRIO_A, TYPE_TELEMETRY: PRIO_B, TYPE_DEPTH: PRIO_C}
DEPTH_FLOOR_FRACTION = 0.30

# Rate presets in BYTES/sec (bridge convention) - NOT bits/sec. See
# bytes_per_sec_to_bps() for the x8 conversion used when publishing budget_bps.
RATE_PRESETS = {"BLE": 3000, "HF": 1250}


def bytes_per_sec_to_bps(bytes_per_sec: float) -> float:
    """bridge.py rate caps are BYTES/sec; core/src/cadence.rs's budget_bps is
    BITS/sec. This is the one and only place that conversion happens -
    callers must not re-derive the x8 by hand."""
    return bytes_per_sec * 8


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
    if frame_type not in (TYPE_DATA, TYPE_TELEMETRY, TYPE_DEPTH):
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


class TokenBucket:
    """Continuous-refill token bucket. rate is BYTES/sec; capacity caps the
    burst (defaults to a 1s burst, i.e. capacity == rate)."""

    def __init__(self, rate_bytes_per_sec: float, capacity_bytes: float = None):
        self.rate = rate_bytes_per_sec
        self.capacity = self.rate if capacity_bytes is None else capacity_bytes
        self.tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def try_consume(self, n: int) -> bool:
        with self._lock:
            self._refill_locked()
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False

    def available(self) -> float:
        with self._lock:
            self._refill_locked()
            return self.tokens


class PriorityScheduler:
    """Outbound (UDP-ingress -> serial/BLE-egress) frame scheduler.

    Priority A (MinBand data, 0x01) beats B (telemetry, 0x02) beats C
    (depth, 0x03), spending from one shared token-bucket rate cap - except
    C also gets its own reserved bucket worth DEPTH_FLOOR_FRACTION of the
    total rate, so a saturated A stream can never fully starve it. Once
    A/B are idle, C may also spend from the shared bucket - the floor is a
    guarantee, not a ceiling.

    ponytail: one scheduler thread + plain deques is plenty for 3 classes
    on a single link; reach for a real WFQ only if priority count or
    per-class fairness needs grow beyond "don't starve the lowest one".
    """

    MAX_QUEUE_FRAMES = 200  # per class; oldest-drops-first backpressure

    def __init__(self, bridge: "Bridge", rate_bytes_per_sec: float,
                 depth_floor_fraction: float = DEPTH_FLOOR_FRACTION):
        self.bridge = bridge
        self.rate = rate_bytes_per_sec
        reserved_c = rate_bytes_per_sec * depth_floor_fraction
        shared = rate_bytes_per_sec - reserved_c
        self.shared_bucket = TokenBucket(shared)
        self.reserved_c_bucket = TokenBucket(reserved_c)
        self.queues = {PRIO_A: collections.deque(), PRIO_B: collections.deque(), PRIO_C: collections.deque()}
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.bytes_sent = {PRIO_A: 0, PRIO_B: 0, PRIO_C: 0}
        self.drops = {PRIO_A: 0, PRIO_B: 0, PRIO_C: 0}
        self.last_rates = {PRIO_A: 0, PRIO_B: 0, PRIO_C: 0}
        self.last_drops = {PRIO_A: 0, PRIO_B: 0, PRIO_C: 0}
        self._stop = False

    def submit(self, frame_type: int, payload: bytes):
        prio = TYPE_TO_PRIORITY[frame_type]
        with self.cv:
            q = self.queues[prio]
            if len(q) >= self.MAX_QUEUE_FRAMES:
                q.popleft()  # drop oldest, make room for newest
                self.drops[prio] += 1
            q.append((frame_type, payload))
            self.cv.notify_all()

    def queue_depths(self):
        with self.lock:
            return {k: len(v) for k, v in self.queues.items()}

    def tick(self):
        """Call ~once/sec (from Bridge.stats_ticker_loop): rolls the
        per-class byte/drop counters into the rate snapshot the HTTP
        telemetry handler reads, same cadence as the rest of Stats."""
        with self.lock:
            self.last_rates = dict(self.bytes_sent)
            self.last_drops = dict(self.drops)
            self.bytes_sent = {PRIO_A: 0, PRIO_B: 0, PRIO_C: 0}
            self.drops = {PRIO_A: 0, PRIO_B: 0, PRIO_C: 0}

    def remaining_bytes_per_sec(self) -> float:
        return self.shared_bucket.available() + self.reserved_c_bucket.available()

    def snapshot_and_reset_bytes(self):
        """Non-1Hz snapshot used by tests/gate runs that don't go through
        the ticker."""
        with self.lock:
            sent, drops = dict(self.bytes_sent), dict(self.drops)
            self.bytes_sent = {PRIO_A: 0, PRIO_B: 0, PRIO_C: 0}
            self.drops = {PRIO_A: 0, PRIO_B: 0, PRIO_C: 0}
            return sent, drops

    def stop(self):
        with self.cv:
            self._stop = True
            self.cv.notify_all()

    def _pick_locked(self):
        # Reserved floor first: if C has backlog and its own reserved
        # bucket can afford it, send it - this is what makes the floor a
        # guarantee rather than a hope.
        c_q = self.queues[PRIO_C]
        if c_q and self.reserved_c_bucket.try_consume(len(c_q[0][1])):
            frame_type, payload = c_q.popleft()
            return PRIO_C, frame_type, payload
        # Otherwise strict priority A > B > C out of the shared bucket. Stop
        # at the first non-empty queue, whether or not it could afford its
        # head frame this round - a smaller lower-priority frame must NOT
        # be allowed to "steal" shared tokens a higher-priority queue is
        # waiting to accumulate, or strict priority silently breaks down
        # into something close to fair-share (this was a real bug caught
        # by the loopback integration test, not just a hypothetical).
        for prio in (PRIO_A, PRIO_B, PRIO_C):
            q = self.queues[prio]
            if q:
                if self.shared_bucket.try_consume(len(q[0][1])):
                    frame_type, payload = q.popleft()
                    return prio, frame_type, payload
                return None
        return None

    def run(self):
        while True:
            with self.cv:
                while not self._stop and not any(self.queues.values()):
                    self.cv.wait(timeout=0.05)
                if self._stop:
                    return
                picked = self._pick_locked()
            if picked is None:
                time.sleep(0.002)  # nothing affordable yet, brief backoff
                continue
            prio, frame_type, payload = picked
            self.bridge.write_frame(frame_type, payload)
            with self.lock:
                self.bytes_sent[prio] += len(payload)


class FakeSerial:
    """In-process stand-in for serial.Serial used by --loopback mode. Two
    instances wired as peers (a.peer = b, b.peer = a) simulate the BLE
    link's write()/read()/in_waiting contract, with injectable loss% and
    one-way latency, so the whole bridge (framing, CRC, scheduler) is
    testable with zero hardware attached.

    ponytail: spawns one throwaway thread per write() to apply latency -
    fine at the frame rates this phase tests at; replace with a timer
    wheel if someone pushes this to thousands of frames/sec.
    """

    def __init__(self, loss_pct: float = 0.0, latency_ms: float = 0.0):
        self.loss_pct = loss_pct
        self.latency_ms = latency_ms
        self.peer = None  # set by caller after both ends exist
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def write(self, data: bytes) -> int:
        if self.loss_pct and random.random() < self.loss_pct:
            return len(data)  # simulated drop on the wire

        def deliver():
            if self.latency_ms:
                time.sleep(self.latency_ms / 1000.0)
            peer = self.peer
            with peer._cv:
                peer._buf.extend(data)
                peer._cv.notify_all()

        threading.Thread(target=deliver, daemon=True).start()
        return len(data)

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        with self._cv:
            if not self._buf:
                self._cv.wait(timeout=0.1)
            n = min(size, len(self._buf))
            if n == 0:
                return b""
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out


class Bridge:
    def __init__(self, ser, udp_sock: socket.socket,
                 udp_forward, quiet: bool, scheduler: "PriorityScheduler" = None,
                 depth_udp_sock: socket.socket = None, udp_forward_depth=None):
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
        # Outbound priority scheduler (None => write_frame() direct, no cap).
        self.scheduler = scheduler
        # bridge-rx demux: second UDP ingress port for depth (priority C),
        # and where received depth frames get forwarded to (recon.py).
        self.depth_udp_sock = depth_udp_sock
        self.udp_forward_depth = udp_forward_depth

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
                elif frame_type == TYPE_DEPTH:
                    # bridge-rx demux: depth frames go to recon.py's target,
                    # a separate logical destination from MinBand data.
                    # Real board RX side only ever reads COM_B (this serial
                    # port) - no COM_A access added here.
                    dest = self.udp_forward_depth
                    if dest is None:
                        continue  # no depth destination configured; drop
                    self.udp_sock.sendto(payload, dest)
                    with self.stats.lock:
                        self.stats.serial_to_udp_bytes += len(payload)
                        self.stats.serial_to_udp_frames += 1

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
            if self.scheduler is not None:
                self.scheduler.submit(TYPE_DATA, data)
            else:
                self.write_frame(TYPE_DATA, data)
            with self.stats.lock:
                self.stats.udp_to_serial_bytes += len(data)
                self.stats.udp_to_serial_frames += 1

    def depth_udp_reader_loop(self):
        """Ingress for priority-C (depth) frames on a second UDP port,
        separate from the MinBand data port above."""
        while True:
            try:
                data, addr = self.depth_udp_sock.recvfrom(2048)
            except OSError:
                return
            if len(data) > MAX_PAYLOAD:
                with self.stats.lock:
                    self.stats.oversize_drops += 1
                continue
            if self.scheduler is not None:
                self.scheduler.submit(TYPE_DEPTH, data)
            else:
                self.write_frame(TYPE_DEPTH, data)
            with self.stats.lock:
                self.stats.udp_to_serial_bytes += len(data)
                self.stats.udp_to_serial_frames += 1

    def stats_ticker_loop(self):
        while True:
            time.sleep(1)
            rates = self.stats.snapshot_and_reset_rates()
            if self.scheduler is not None:
                self.scheduler.tick()
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
                    + (f" | sched A={self.scheduler.last_rates[PRIO_A]}B/s "
                       f"B={self.scheduler.last_rates[PRIO_B]}B/s "
                       f"C={self.scheduler.last_rates[PRIO_C]}B/s "
                       f"budget_bps={bytes_per_sec_to_bps(self.scheduler.remaining_bytes_per_sec()):.0f}"
                       if self.scheduler is not None else "")
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
                if bridge.scheduler is not None:
                    sched = bridge.scheduler
                    remaining = sched.remaining_bytes_per_sec()
                    qd = sched.queue_depths()
                    payload["scheduler"] = {
                        "rate_preset_bytes_per_sec": sched.rate,
                        "bytes_per_sec": {
                            "data": sched.last_rates[PRIO_A],
                            "telemetry": sched.last_rates[PRIO_B],
                            "depth": sched.last_rates[PRIO_C],
                        },
                        "queue_depth": {
                            "data": qd[PRIO_A], "telemetry": qd[PRIO_B], "depth": qd[PRIO_C],
                        },
                        "drops": {
                            "data": sched.last_drops[PRIO_A],
                            "telemetry": sched.last_drops[PRIO_B],
                            "depth": sched.last_drops[PRIO_C],
                        },
                        "remaining_bytes_per_sec": remaining,
                        # core/src/cadence.rs's budget_bps is BITS/sec; this
                        # bridge's rate caps are BYTES/sec - the x8 here is
                        # the one and only place that conversion happens.
                        "budget_bps": bytes_per_sec_to_bps(remaining),
                    }
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

    # TYPE_DEPTH (0x03) decode - new frame type must be on the allow-list,
    # same as TYPE_DATA/TYPE_TELEMETRY, no seq field (not TYPE_DATA).
    depth_payload = bytes(range(155))  # ~155B, the depth-chunk phase's size
    encoded_depth = slip_encode(TYPE_DEPTH, depth_payload)
    frames = SlipFramer().feed(encoded_depth)
    if len(frames) != 1:
        failures.append(f"depth decode: expected 1 frame, got {len(frames)}")
    else:
        t, seq, p = slip_decode(frames[0])
        if t != TYPE_DEPTH or p != depth_payload:
            failures.append("depth decode: type/payload mismatch")

    # TokenBucket: fills, empties, and refills at the configured rate.
    bucket = TokenBucket(rate_bytes_per_sec=1000, capacity_bytes=1000)
    if not bucket.try_consume(1000):
        failures.append("TokenBucket: full bucket should allow consuming capacity")
    if bucket.try_consume(1):
        failures.append("TokenBucket: empty bucket should refuse to consume")
    time.sleep(0.05)
    avail = bucket.available()
    if not (30 <= avail <= 70):  # ~50 tokens expected (1000 B/s * 0.05s), tolerant window
        failures.append(f"TokenBucket: refill after 50ms expected ~50 tokens, got {avail:.1f}")

    # The x8 BYTES/sec -> BITS/sec budget_bps conversion - this exact unit
    # trap (core/src/cadence.rs budget_bps is bits/sec, this bridge's rate
    # caps are bytes/sec) bit the project before, so assert it explicitly
    # rather than trusting the arithmetic was done correctly by eye.
    for bytes_per_sec, want_bps in (
        (RATE_PRESETS["BLE"], 24000), (RATE_PRESETS["HF"], 10000), (500, 4000), (0, 0),
    ):
        got = bytes_per_sec_to_bps(bytes_per_sec)
        if got != want_bps or got != bytes_per_sec * 8:
            failures.append(
                f"budget_bps x8 conversion: {bytes_per_sec} B/s -> expected {want_bps} bps, got {got}"
            )

    if failures:
        for f in failures:
            print("FAIL:", f, file=sys.stderr)
        return 1
    print("selftest OK")
    return 0


def run_priority_floor_gate(duration_s: float = 5.0, rate: float = 2000.0) -> bool:
    """GATE: under sustained Priority-A saturation, Priority-C must still
    get ~DEPTH_FLOOR_FRACTION of the bandwidth over the window. No sockets,
    no serial - exercises PriorityScheduler/TokenBucket directly."""

    class _NullBridge:
        def write_frame(self, frame_type, payload):
            pass  # scheduler unit test: delivery itself isn't under test here

    sched = PriorityScheduler(_NullBridge(), rate)
    threading.Thread(target=sched.run, daemon=True).start()

    stop = False

    def saturate_a():
        while not stop:
            sched.submit(TYPE_DATA, bytes(64))

    def feed_c():
        while not stop:
            sched.submit(TYPE_DEPTH, bytes(64))
            time.sleep(0.01)

    threading.Thread(target=saturate_a, daemon=True).start()
    threading.Thread(target=feed_c, daemon=True).start()
    time.sleep(duration_s)
    stop = True
    sched.stop()

    sent, _ = sched.snapshot_and_reset_bytes()
    total = sent[PRIO_A] + sent[PRIO_C]
    c_share = sent[PRIO_C] / total if total else 0.0
    print(
        f"[gate] priority floor @ {rate:.0f}B/s over {duration_s:.0f}s: "
        f"A={sent[PRIO_A]}B C={sent[PRIO_C]}B total={total}B C_share={c_share:.1%} "
        f"(floor target {DEPTH_FLOOR_FRACTION:.0%})"
    )
    # Small tolerance below the exact floor for scheduling/timing jitter.
    passed = c_share >= DEPTH_FLOOR_FRACTION - 0.05
    print(f"[gate] priority floor: {'PASS' if passed else 'FAIL'}")
    return passed


def run_loopback_gate() -> bool:
    """GATE: loopback run at HF preset (1250 B/s) with 5% artificial loss,
    sustaining traffic on both priority classes at once, process survives,
    neither class fully starves. Preceded by the mandatory 40B sanity
    round-trip (catches port-mismatch-class bugs before the bigger test).
    No hardware: FakeSerial stands in for the BLE link."""
    loss_pct = 0.05
    rate = RATE_PRESETS["HF"]

    ser_a, ser_b = FakeSerial(loss_pct, latency_ms=5.0), FakeSerial(loss_pct, latency_ms=5.0)
    ser_a.peer, ser_b.peer = ser_b, ser_a

    udp_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); udp_a.bind(("127.0.0.1", 0))
    udp_a.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    depth_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); depth_a.bind(("127.0.0.1", 0))
    udp_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); udp_b.bind(("127.0.0.1", 0))
    data_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); data_recv.bind(("127.0.0.1", 0))
    depth_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); depth_recv.bind(("127.0.0.1", 0))
    data_recv.settimeout(0.2)
    depth_recv.settimeout(0.2)

    bridge_a = Bridge(ser_a, udp_a, None, quiet=True)
    scheduler = PriorityScheduler(bridge_a, rate)
    bridge_a.scheduler = scheduler
    bridge_a.depth_udp_sock = depth_a
    bridge_b = Bridge(ser_b, udp_b, data_recv.getsockname(), quiet=True,
                       udp_forward_depth=depth_recv.getsockname())

    threads = [
        threading.Thread(target=bridge_a.serial_reader_loop, daemon=True),
        threading.Thread(target=bridge_a.udp_reader_loop, daemon=True),
        threading.Thread(target=bridge_a.depth_udp_reader_loop, daemon=True),
        threading.Thread(target=scheduler.run, daemon=True),
        threading.Thread(target=bridge_b.serial_reader_loop, daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(0.2)

    data_port = udp_a.getsockname()[1]
    depth_port = depth_a.getsockname()[1]
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # --- mandatory 40B sanity round-trip, BOTH classes, before anything bigger ---
    # This checks wiring (right frame type -> right UDP destination), not
    # loss tolerance, so retry a handful of times: with loss_pct=5% active
    # a single send has a real (if small) chance of being the one dropped.
    def sanity_round_trip(port, recv_sock, attempts=5) -> bool:
        for _ in range(attempts):
            sender.sendto(bytes(40), ("127.0.0.1", port))
            try:
                recv_sock.recvfrom(2048)
                return True
            except socket.timeout:
                continue
        return False

    data_sanity_ok = sanity_round_trip(data_port, data_recv)
    depth_sanity_ok = sanity_round_trip(depth_port, depth_recv)
    sanity_ok = data_sanity_ok and depth_sanity_ok
    print(f"[gate] 40B sanity round-trip: data={'PASS' if data_sanity_ok else 'FAIL'} "
          f"depth={'PASS' if depth_sanity_ok else 'FAIL'}")

    # --- sustained HF-preset, 5% loss, both classes at once ---
    duration = 6.0
    stop = False

    def blast_a():
        # ~2000 pkt/s offered (240000 B/s) - far above the 1250 B/s cap, so
        # Priority-A stays genuinely backlogged throughout. A coarser pacer
        # (e.g. time.sleep(0.02)) undershoots badly on Windows' ~15ms timer
        # tick and never actually saturates the link - this tighter one does.
        while not stop:
            sender.sendto(bytes(120), ("127.0.0.1", data_port))
            time.sleep(0.0005)

    def blast_c():
        while not stop:
            sender.sendto(bytes(60), ("127.0.0.1", depth_port))
            time.sleep(0.05)  # modest, steady depth traffic

    recv_counts = {"data": 0, "data_bytes": 0, "depth": 0, "depth_bytes": 0}

    def recv_data():
        while not stop:
            try:
                d, _ = data_recv.recvfrom(2048)
            except socket.timeout:
                continue
            recv_counts["data"] += 1
            recv_counts["data_bytes"] += len(d)

    def recv_depth():
        while not stop:
            try:
                d, _ = depth_recv.recvfrom(2048)
            except socket.timeout:
                continue
            recv_counts["depth"] += 1
            recv_counts["depth_bytes"] += len(d)

    workers = [threading.Thread(target=f, daemon=True) for f in (blast_a, blast_c, recv_data, recv_depth)]
    for w in workers:
        w.start()
    time.sleep(duration)
    stop = True
    time.sleep(0.3)
    scheduler.stop()

    total_bytes = recv_counts["data_bytes"] + recv_counts["depth_bytes"]
    data_bps = recv_counts["data_bytes"] / duration
    depth_bps = recv_counts["depth_bytes"] / duration
    depth_share = recv_counts["depth_bytes"] / total_bytes if total_bytes else 0.0
    print(
        f"[gate] loopback HF-preset({rate}B/s) loss={loss_pct:.0%} over {duration:.0f}s: "
        f"data={recv_counts['data']}f/{recv_counts['data_bytes']}B ({data_bps:.0f} B/s) "
        f"depth={recv_counts['depth']}f/{recv_counts['depth_bytes']}B ({depth_bps:.0f} B/s) "
        f"depth_share={depth_share:.1%}"
    )
    both_classes_flowed = recv_counts["data"] > 0 and recv_counts["depth"] > 0
    print(f"[gate] both classes delivered traffic, no starvation, no crash: "
          f"{'PASS' if both_classes_flowed else 'FAIL'}")

    # Not closing these sockets explicitly: serial_reader_loop has no clean
    # shutdown signal (by design - the real bridge runs forever until
    # Ctrl-C), so a daemon thread can still be mid-sendto() on one of them;
    # closing here just to "be tidy" raced and threw on teardown. Process
    # exit after this function returns reclaims them anyway.
    return sanity_ok and both_classes_flowed


def run_gate() -> int:
    ok = run_priority_floor_gate()
    ok = run_loopback_gate() and ok
    return 0 if ok else 1


def resolve_rate(args) -> float:
    if args.rate_bytes_per_sec:
        return args.rate_bytes_per_sec
    if args.rate_preset:
        return RATE_PRESETS[args.rate_preset]
    return None


def run_loopback(args) -> int:
    """--loopback: both bridge ends in one process over a FakeSerial pair -
    no COM port, no boards. UDP sockets are real, so anything that talks
    UDP (oneway_test.py-style senders, recon.py, MinBand) can point at the
    ports this prints without knowing it's not talking to real hardware."""
    ser_a, ser_b = FakeSerial(args.loopback_loss, args.loopback_latency_ms), \
        FakeSerial(args.loopback_loss, args.loopback_latency_ms)
    ser_a.peer, ser_b.peer = ser_b, ser_a

    udp_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_a.bind(("0.0.0.0", args.udp_listen or 0))
    depth_a = None
    if args.udp_listen_depth:
        depth_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        depth_a.bind(("0.0.0.0", args.udp_listen_depth))

    rate = resolve_rate(args)
    bridge_a = Bridge(ser_a, udp_a, None, args.quiet)
    scheduler = None
    if rate:
        scheduler = PriorityScheduler(bridge_a, rate)
        bridge_a.scheduler = scheduler
    bridge_a.depth_udp_sock = depth_a

    udp_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_b.bind(("0.0.0.0", 0))
    udp_forward = parse_host_port(args.udp_forward) if args.udp_forward else None
    udp_forward_depth = parse_host_port(args.udp_forward_depth) if args.udp_forward_depth else None
    bridge_b = Bridge(ser_b, udp_b, udp_forward, args.quiet, udp_forward_depth=udp_forward_depth)

    threads = [
        threading.Thread(target=bridge_a.serial_reader_loop, daemon=True),
        threading.Thread(target=bridge_a.udp_reader_loop, daemon=True),
        threading.Thread(target=bridge_a.stats_ticker_loop, daemon=True),
        threading.Thread(target=bridge_b.serial_reader_loop, daemon=True),
        # bridge_b's own udp_reader_loop (P5a bugfix): without this, replies sent back by
        # --udp-forward's target (e.g. the MinBand server's Ack) land on udp_b and are never
        # read, so they never get relayed over the fake serial link back to side A. A live
        # WasmEdge stuck waiting for its first Ack (core/src/edge.rs:403) then repeats Hello
        # forever and never reaches Delta/Keyframe -- caught via P5a's bridge-loopback gate.
        threading.Thread(target=bridge_b.udp_reader_loop, daemon=True),
        threading.Thread(target=bridge_b.stats_ticker_loop, daemon=True),
    ]
    if depth_a is not None:
        threads.append(threading.Thread(target=bridge_a.depth_udp_reader_loop, daemon=True))
    if scheduler is not None:
        threads.append(threading.Thread(target=scheduler.run, daemon=True))
    for t in threads:
        t.start()

    if args.http_port:
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", args.http_port), bridge_a.make_http_handler())
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print(f"[loopback] data-in :{udp_a.getsockname()[1]} -> forward {udp_forward} "
          f"loss={args.loopback_loss:.0%} latency={args.loopback_latency_ms:.0f}ms "
          f"rate={rate}", file=sys.stderr)
    if depth_a is not None:
        print(f"[loopback] depth-in :{depth_a.getsockname()[1]} -> forward {udp_forward_depth}",
              file=sys.stderr)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
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
    ap.add_argument("--selftest-gate", action="store_true",
                     help="run the token-bucket/priority-floor + loopback gate tests and exit")
    ap.add_argument("--rate-preset", choices=sorted(RATE_PRESETS),
                     help="enable the outbound priority scheduler, capped at this preset's B/s")
    ap.add_argument("--rate-bytes-per-sec", type=float,
                     help="custom outbound rate cap in BYTES/sec (overrides --rate-preset)")
    ap.add_argument("--udp-listen-depth", type=int,
                     help="2nd UDP ingress port for TYPE_DEPTH (priority C) frames")
    ap.add_argument("--udp-forward-depth",
                     help="host:port depth frames are forwarded to on the receive side "
                          "(e.g. recon.py at 127.0.0.1:7790)")
    ap.add_argument("--loopback", action="store_true",
                     help="run both bridge ends in one process over a fake serial link - "
                          "no boards, no COM port needed")
    ap.add_argument("--loopback-loss", type=float, default=0.0,
                     help="loopback only: fraction of frames dropped on the fake link (0..1)")
    ap.add_argument("--loopback-latency-ms", type=float, default=0.0,
                     help="loopback only: one-way delay added by the fake link")
    args = ap.parse_args()

    if args.selftest:
        return run_selftest()
    if args.selftest_gate:
        return run_gate()
    if args.loopback:
        return run_loopback(args)

    if not args.serial:
        ap.error("--serial is required (unless --selftest/--selftest-gate/--loopback)")

    ser = serial.Serial(args.serial, args.baud, timeout=0.1)

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.udp_listen:
        udp_sock.bind(("0.0.0.0", args.udp_listen))
    else:
        udp_sock.bind(("0.0.0.0", 0))

    depth_sock = None
    if args.udp_listen_depth:
        depth_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        depth_sock.bind(("0.0.0.0", args.udp_listen_depth))

    udp_forward = parse_host_port(args.udp_forward) if args.udp_forward else None
    udp_forward_depth = parse_host_port(args.udp_forward_depth) if args.udp_forward_depth else None

    rate = resolve_rate(args)
    bridge = Bridge(ser, udp_sock, udp_forward, args.quiet,
                     depth_udp_sock=depth_sock, udp_forward_depth=udp_forward_depth)
    scheduler = None
    if rate:
        scheduler = PriorityScheduler(bridge, rate)
        bridge.scheduler = scheduler

    threads = [
        threading.Thread(target=bridge.serial_reader_loop, daemon=True),
        threading.Thread(target=bridge.udp_reader_loop, daemon=True),
        threading.Thread(target=bridge.stats_ticker_loop, daemon=True),
    ]
    if depth_sock is not None:
        threads.append(threading.Thread(target=bridge.depth_udp_reader_loop, daemon=True))
    if scheduler is not None:
        threads.append(threading.Thread(target=scheduler.run, daemon=True))
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
