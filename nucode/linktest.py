#!/usr/bin/env python3
"""Throughput / RTT / loss test through the real BLE bridge link.

Usage:
    python linktest.py --to 127.0.0.1:7788 --echo-port 7777 \
        --size 40 --count 500 [--rate 50]

Two sockets, both local:
  - sender socket -> bridge A's listen port (--to)
  - echo socket bound on --echo-port, standing in for the real server;
    bounces each datagram straight back.

Reply path: echo socket -> bridge B -> BLE -> bridge A -> sender socket.
Payload carries seq:u32 + monotonic_ns:u64 + padding to --size, so RTT
needs no clock sync (we measure elapsed time locally with perf_counter_ns).
"""
import argparse
import socket
import struct
import sys
import time

HEADER = struct.Struct("<IQ")  # seq:u32, monotonic_ns:u64 (unused on wire, kept for parity)


def run_echo_responder(echo_port: int, stop_event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", echo_port))
    sock.settimeout(0.2)
    while not stop_event["stop"]:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        sock.sendto(data, addr)
    sock.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="host:port of bridge A's UDP listen")
    ap.add_argument("--echo-port", type=int, required=True,
                     help="local port to bind for the echo responder (bridge B forwards here)")
    ap.add_argument("--size", type=int, default=40)
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--rate", type=float, default=0, help="datagrams/sec, 0 = as fast as possible")
    args = ap.parse_args()

    to_host, to_port = args.to.rsplit(":", 1)
    to_addr = (to_host, int(to_port))

    import threading
    stop_event = {"stop": False}
    echo_thread = threading.Thread(target=run_echo_responder, args=(args.echo_port, stop_event), daemon=True)
    echo_thread.start()
    time.sleep(0.3)  # let the responder bind before we start sending

    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.settimeout(0.5)

    payload_size = max(args.size, HEADER.size)
    padding = bytes(payload_size - HEADER.size)

    rtts_ns = []
    sent = 0
    received = 0
    last_seq_received = -1
    reorder_count = 0

    interval = (1.0 / args.rate) if args.rate > 0 else 0
    start = time.perf_counter_ns()

    for seq in range(args.count):
        send_time = time.perf_counter_ns()
        packet = HEADER.pack(seq, send_time) + padding
        sender.sendto(packet, to_addr)
        sent += 1

        try:
            data, _ = sender.recvfrom(2048)
            recv_time = time.perf_counter_ns()
            r_seq, r_send_time = HEADER.unpack_from(data)
            rtts_ns.append(recv_time - r_send_time)
            received += 1
            if r_seq < last_seq_received:
                reorder_count += 1
            last_seq_received = r_seq
        except socket.timeout:
            pass

        if interval:
            elapsed = (time.perf_counter_ns() - send_time) / 1e9
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

    total_elapsed_s = (time.perf_counter_ns() - start) / 1e9
    stop_event["stop"] = True

    loss_pct = 100.0 * (sent - received) / sent if sent else 0.0
    kbps = (received * payload_size / 1024.0) / total_elapsed_s if total_elapsed_s > 0 else 0.0

    if rtts_ns:
        sorted_rtts = sorted(rtts_ns)
        p50 = sorted_rtts[len(sorted_rtts) // 2] / 1e6
        p95 = sorted_rtts[int(len(sorted_rtts) * 0.95)] / 1e6
    else:
        p50 = p95 = float("nan")

    print(f"size={payload_size}B count={args.count} rate={args.rate or 'max'}")
    print(f"sent={sent} received={received} loss={loss_pct:.2f}% reorder={reorder_count}")
    print(f"throughput={kbps:.2f} kB/s")
    print(f"RTT p50={p50:.2f}ms p95={p95:.2f}ms")

    return 0


if __name__ == "__main__":
    sys.exit(main())
