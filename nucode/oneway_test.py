#!/usr/bin/env python3
"""One-way throughput/loss/latency test through the real BLE bridge link.

Unlike linktest.py (which measures RTT via a local echo responder), this
sends datagrams in one direction only - TX(central)->RX(peripheral), the
direction that uses BLE write-without-response (clientUart.write() in
tx_central.ino), not peripheral->central notify (the one with the HVN
credit-starvation problem documented in README.md). Sender and receiver
run on the same host, so a shared wall clock (time.time_ns()) gives a
real one-way latency without needing a round trip.

Usage:
    python bridge.py --serial <TX_COM> --udp-listen 7788              # bridge A
    python bridge.py --serial <RX_COM> --udp-forward 127.0.0.1:7779   # bridge B
    python oneway_test.py --to 127.0.0.1:7788 --recv-port 7779 --sweep
    python oneway_test.py --to 127.0.0.1:7788 --recv-port 7779 --soak
"""
import argparse
import socket
import struct
import sys
import threading
import time

HEADER = struct.Struct("<IQ")  # seq:u32, send_time_ns:u64 (shared wall clock)
SWEEP_RATES_KBPS = (2, 4, 8, 12, 16)
SOAK_RATE_KBPS = 8
SOAK_DURATION_S = 600.0
LOSS_PASS_PCT = 0.1
P95_PASS_MS = 200.0


class Receiver:
    def __init__(self, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.2)
        self.lock = threading.Lock()
        self.latencies_ms = []
        self.seqs_seen = set()
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            recv_ns = time.time_ns()
            if len(data) < HEADER.size:
                continue
            seq, send_ns = HEADER.unpack_from(data)
            with self.lock:
                self.latencies_ms.append((recv_ns - send_ns) / 1e6)
                self.seqs_seen.add(seq)

    def close(self):
        self._stop = True
        self.sock.close()


def percentile(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def run_one(to_addr, recv_port, size, rate_kbps, duration_s, label):
    receiver = Receiver(recv_port)
    th = threading.Thread(target=receiver.run, daemon=True)
    th.start()
    time.sleep(0.3)  # let the receiver bind before we start sending

    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pps = (rate_kbps * 1000) / size
    interval = 1.0 / pps
    n_packets = int(duration_s * pps)
    pad = bytes(max(0, size - HEADER.size))

    next_send = time.perf_counter()
    for seq in range(n_packets):
        payload = HEADER.pack(seq & 0xFFFFFFFF, time.time_ns()) + pad
        sender.sendto(payload, to_addr)
        next_send += interval
        sleep_for = next_send - time.perf_counter()
        if sleep_for > 0:
            time.sleep(sleep_for)

    time.sleep(1.0)  # drain whatever's still in flight over BLE
    receiver.close()

    with receiver.lock:
        received = len(receiver.seqs_seen)
        latencies = list(receiver.latencies_ms)

    sent = n_packets
    loss_pct = 100.0 * (sent - received) / sent if sent else 0.0
    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    throughput_kbps = (received * size) / 1000.0 / duration_s if duration_s else 0.0
    passed = loss_pct <= LOSS_PASS_PCT and p95 < P95_PASS_MS
    status = "PASS" if passed else "FAIL"

    print(
        f"[{label}] rate={rate_kbps}kB/s size={size}B duration={duration_s:.0f}s "
        f"sent={sent} received={received} loss={loss_pct:.3f}% "
        f"throughput={throughput_kbps:.2f}kB/s p50={p50:.2f}ms p95={p95:.2f}ms "
        f"-> {status}"
    )
    return {
        "label": label, "rate_kbps": rate_kbps, "sent": sent, "received": received,
        "loss_pct": loss_pct, "p50_ms": p50, "p95_ms": p95, "passed": passed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="host:port of bridge A's UDP listen")
    ap.add_argument("--recv-port", type=int, required=True,
                     help="local port bridge B's --udp-forward points at")
    ap.add_argument("--size", type=int, default=200, help="datagram size, max 200B")
    ap.add_argument("--duration", type=float, default=10.0,
                     help="seconds per sweep step (ignored for --soak)")
    ap.add_argument("--sweep", action="store_true",
                     help=f"run {SWEEP_RATES_KBPS} kB/s sweep")
    ap.add_argument("--soak", action="store_true",
                     help=f"{SOAK_DURATION_S/60:.0f}-minute soak at {SOAK_RATE_KBPS}kB/s")
    ap.add_argument("--rate", type=float, help="single custom rate in kB/s")
    args = ap.parse_args()

    if args.size > 200:
        ap.error("--size must be <=200B (MinBand max datagram)")
    if not (args.sweep or args.soak or args.rate is not None):
        ap.error("specify --sweep, --soak, or --rate")

    to_host, to_port = args.to.rsplit(":", 1)
    to_addr = (to_host, int(to_port))

    results = []
    if args.sweep:
        for rate in SWEEP_RATES_KBPS:
            results.append(run_one(to_addr, args.recv_port, args.size, rate,
                                    args.duration, f"sweep-{rate}kBps"))
            time.sleep(1)
    if args.soak:
        results.append(run_one(to_addr, args.recv_port, args.size, SOAK_RATE_KBPS,
                                SOAK_DURATION_S, f"soak-{SOAK_RATE_KBPS}kBps-10min"))
    if args.rate is not None:
        results.append(run_one(to_addr, args.recv_port, args.size, args.rate,
                                args.duration, f"custom-{args.rate}kBps"))

    return 1 if any(not r["passed"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
