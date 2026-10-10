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
write-stall fix applied, `bridge.py` reporting per-frame `seq_lost` in
addition to `crc_errors`:

| Payload | Count | Rate | Loss | Reorder | Throughput | RTT p50 | RTT p95 | crc_errors | seq_lost |
|---|---|---|---|---|---|---|---|---|---|
| 40 B | 300 | max | 0% | 0 | 0.68 kB/s | 59.10 ms | 100.11 ms | 0 | 0 |
| 200 B | 300 | max | 0% | 0 | 3.26 kB/s | 59.73 ms | 100.56 ms | 0 | 0 |
| 1000 B | 300 | max | 93.67% | 0 | 0.13 kB/s | 242.91 ms | 392.48 ms | 280 | 280 |
| 1200 B (MinBand keyframe size) | 300 | max | 0% | 0 | 4.76 kB/s | 239.86 ms | 355.48 ms | +1 | +1 |

**Root cause (confirmed via `BLECharacteristic::notify()` source in the
installed SDK):** peripheral->central BLE notify draws from a small fixed
SoftDevice credit pool (`_hvn_sem`), refilled only on
`BLE_GATTS_EVT_HVN_TX_COMPLETE`. A 1000 B datagram fragments into ~5 BLE
notifications; under sustained back-to-back 1000 B traffic the credit pool
exhausts faster than it refills, `notify()` blocks past its timeout, and
the existing write-stall-then-resync-drop path (added for an earlier bug)
kicks in repeatedly — this is what shows up as climbing `crc_errors`.
Confirmed self-recovering, not a stuck connection: a clean 40 B burst run
immediately after a lossy 1000 B burst, on the same BLE connection, comes
back at 0% loss instantly. Central->peripheral writes (ATT write command)
never show this problem, only peripheral->central notify does.

By design (fire-and-forget, no ACK/retry/credit scheme — see decision
below), `crc_errors` and `seq_lost` land on the same 280 frames in the
1000 B run: every notify-stall drop here corrupted-and-got-caught by CRC,
none vanished without a trace. `seq_lost` exists to also catch the other
failure mode (a frame disappearing cleanly, with no corrupt bytes left for
CRC to flag) if it ever shows up as a non-zero *difference* from
`crc_errors` — it didn't in this pass, but the instrumentation is now
always-on via `/telemetry`.

MinBand's actual payloads are mostly 40-70 B deltas with rare ~1200 B
keyframes — both measured here at 0% loss. The 1000 B ceiling is a real,
root-caused finding, not a blocker for MinBand's traffic shape.

**Design decision (made with Piotrek):** no ACK/retry/credit scheme for
now — the link is a tough RF environment and the priority is throughput
plus the ability to *see and quantify* loss, not guaranteed delivery.
`bridge.py` stamps a wrapping u16 sequence number on every `TYPE_DATA`
frame; the receiving side reports `seq_lost` via `/telemetry` alongside
`crc_errors`, `oversize_drops`, `unknown_type_drops`. Pure `bridge.py`
change, no firmware reflash — the sequence number rides inside the
existing SLIP frame body, firmware stays a byte-transparent pipe.

**Raw BLE 2M PHY baseline** (stock Adafruit `Peripheral/throughput` +
`Central/throughput` examples, unidirectional notify blast, no SLIP/CRC
framing): 244000 B in 3.52 s = **69.42 KB/s**. Note: the stock
`Central/throughput` example's GATT service discovery is flaky on this
board/core combo (needed a UUID scan filter + 500 ms post-connect delay
to reliably discover the UART service — same class of issue as the
scanner bug fixed in `tx_central.ino`).
