# NUCODE NU-40 BLE link

Two NU-40 (nRF52840) boards act as a UDP-over-BLE bridge: one TX (central),
one RX (peripheral), bleuart serial pipe between them, 2M PHY, MTU 247.

## Hardware / toolchain

- Board: NUCODE NU-40 (nRF52840), FQBN `nucode:nrf52:nu40dk`
- arduino-cli 1.5.1, NUCODE core `nucode:nrf52@1.0.2`
- Flash/reset: USB only, double-tap RESET for bootloader. No J-Link, no soldering.
- `boards.json` maps each board's USB serial number to its `last_known_port`
  (COM numbers aren't stable across reflashes — re-resolve via
  `arduino-cli board list --format json` or
  `serial.tools.list_ports.comports()` matching on `serial_number` if a board
  doesn't show up on the expected port).

## Flash

```
python flash.py tx   # fw/tx_central -> TX board
python flash.py rx   # fw/rx_peripheral -> RX board
```

## Run the bridge

```
python bridge.py --serial <RX_COM> --udp-forward 127.0.0.1:7777 --http-port 28766   # bridge B (RX side)
python bridge.py --serial <TX_COM> --udp-listen 7788 --http-port 28765              # bridge A (TX side)
```

Ports here (7788/7777) must match whatever `linktest.py --to`/`--echo-port`
you run against it (see its own docstring for the default) - a mismatch
here silently produces 100% loss with no error, since bridge B just
forwards into a void. Always check with a small (40B) run first.

UDP traffic sent to bridge A's `--udp-listen` port goes out over BLE to
bridge B, which forwards it to `--udp-forward`; replies flow back over the
same path (bridge A remembers the last peer, no fixed forward address).
`GET /telemetry` on either bridge's `--http-port` reports live stats:
`crc_errors`/`oversize_drops`/`unknown_type_drops` (framing-level faults),
`seq_lost` (cumulative count of TYPE_DATA frames that never arrived at
all, inferred from gaps in each frame's wire sequence number - catches
loss that leaves no corrupt bytes behind for CRC to flag), `seq_last`
(highest sequence number seen so far).

## Measurements (2026-10-10)

`linktest.py` (sender + echo responder, RTT via `perf_counter_ns`) over the
real BLE link, `rx_peripheral`/`tx_central` firmware with the BLE
write-stall fix applied:

| Payload | Count | Rate | Loss | Reorder | Throughput | RTT p50 | RTT p95 |
|---|---|---|---|---|---|---|---|
| 40 B | 500 | max | 0% | 0 | 0.81 kB/s | 40.09 ms | 79.94 ms |
| 200 B | 500 | max | 0% | 0 | 4.08 kB/s | 40.19 ms | 79.90 ms |
| 1000 B | 300 | max | 91.00% | 0 | 0.18 kB/s | 253.32 ms | 417.13 ms |
| 1000 B | 100 | 5/s | 91.00% | 0 | 0.18 kB/s | 248.10 ms | 439.72 ms |

1000 B loss is accompanied by growing CRC errors on bridge A's
BLE->serial path (crc_err climbing continuously during the run) — this
looks like SLIP frame corruption from large-payload reassembly across
multiple BLE notifications, not just congestion, since the paced (5/s)
run shows the same loss rate as max-rate. MinBand's actual payloads are
mostly 40-70 B deltas with rare ~1200 B keyframes, so this 1000B ceiling
is a reportable finding, not a blocker — not pursued further here per
scope (root-causing the SLIP/large-payload interaction would mean
touching the write-stall recovery logic, out of scope for this pass).

**Raw BLE 2M PHY baseline** (stock Adafruit `Peripheral/throughput` +
`Central/throughput` examples, unidirectional notify blast, no SLIP/CRC
framing): 244000 B in 3.52 s = **69.42 KB/s**. Note: the stock
`Central/throughput` example's GATT service discovery is flaky on this
board/core combo (needed a UUID scan filter + 500 ms post-connect delay
to reliably discover the UART service — same class of issue as the
scanner bug fixed in `tx_central.ino`).
