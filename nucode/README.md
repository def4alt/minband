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

| Payload | Count | Rate | Loss | Reorder | Throughput | RTT p50 | RTT p95 |
|---|---|---|---|---|---|---|---|
| 40 B | 300 | max | 0% | 0 | 0.68 kB/s | 59.10 ms | 100.11 ms |
| 200 B | 300 | max | 0% | 0 | 3.26 kB/s | 59.73 ms | 100.56 ms |
| 1000 B (run 1/3) | 300 | max | 93.67% | 0 | 0.13 kB/s | 242.91 ms | 392.48 ms |
| 1000 B (run 2/3) | 300 | max | 88.00% | 0 | 0.25 kB/s | 237.51 ms | 379.99 ms |
| 1000 B (run 3/3) | 300 | max | 89.67% | 0 | 0.21 kB/s | 237.64 ms | 357.93 ms |
| 1200 B (MinBand keyframe, run 1/3) | 300 | max | 38.00% | 0 | 1.87 kB/s | 321.47 ms | 500.93 ms |
| 1200 B (MinBand keyframe, run 2/3) | 300 | max | 0.33% | 0 | 7.36 kB/s | 142.19 ms | 200.71 ms |
| 1200 B (MinBand keyframe, run 3/3) | 300 | max | 0.00% | 0 | 7.56 kB/s | 142.74 ms | 200.40 ms |

Across these repeats `crc_errors` tracked `seq_lost` closely (within a few
frames per run, never a large gap) — losses are overwhelmingly
corrupted-and-caught by CRC, not clean vanishes.

**Root cause (confirmed via `BLECharacteristic::notify()` source in the
installed SDK):** peripheral->central BLE notify draws from a small fixed
SoftDevice credit pool (`_hvn_sem`), refilled only on
`BLE_GATTS_EVT_HVN_TX_COMPLETE`. Larger datagrams fragment into more BLE
notifications (1000 B into ~5, 1200 B into ~6); under sustained
back-to-back traffic the credit pool can exhaust faster than it refills,
`notify()` blocks past its timeout, and the existing
write-stall-then-resync-drop path (added for an earlier bug) kicks in —
this is what shows up as climbing `crc_errors`. Confirmed self-recovering,
not a stuck connection: a clean 40 B burst run immediately after a lossy
1000 B burst, on the same BLE connection, comes back at 0% loss instantly.
Central->peripheral writes (ATT write command) never show this problem,
only peripheral->central notify does.

**Important: this is not a hard byte-size cutoff.** Three repeated 1200 B
runs gave 38%, 0.33%, and 0% loss — wildly different results for the same
payload size on the same link. Whether the credit pool runs dry depends on
timing against live radio conditions (ongoing low-level BLE retransmits,
connection interval phase), not on payload size alone — size just changes
how many fragments are in flight and therefore how likely a given burst is
to hit a bad timing window. 1000 B was consistently bad across all three
repeats (88-94%); 1200 B was not consistently good. Do not treat a single
clean run at any size as proof that size is safe — always run several
repeats before relying on a number.

MinBand's actual payloads are mostly 40-70 B deltas, measured here at 0%
loss across the board. The ~1200 B keyframe size is NOT reliably safe at
this notify rate — expect occasional bursts of significant loss, not a
guaranteed ceiling.

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

## Coded PHY (Long Range) (2026-10-10)

Both `tx_central.ino` and `rx_peripheral.ino` request
`BLE_GAP_PHY_CODED` instead of `BLE_GAP_PHY_2MBPS` in their
post-connect callback (`conn->requestPHY(...)`, same call site used for
2M PHY). Advertising/scanning still happen on legacy 1M PHY — only
`requestPHY()` is changed after a connection is already established.
Bluefruit's `BLEConnection::requestPHY()` binding does not expose a
coding-scheme parameter (S=2 vs S=8); the SoftDevice picks it
internally, out of reach without bypassing the Bluefruit wrapper.
Confirmed active indirectly, via the expected throughput/RTT hit (no
firmware-side logging added — the production USB serial port is the
live SLIP/CRC data pipe, see Gotchas below):

| Payload | Count | Rate | Loss | Reorder | Throughput | RTT p50 | RTT p95 |
|---|---|---|---|---|---|---|---|
| 40 B (run 1/3) | 300 | max | 0% | 0 | 0.82 kB/s | 40.12 ms | 79.88 ms |
| 40 B (run 2/3) | 300 | max | 0% | 0 | 0.69 kB/s | 50.47 ms | 99.96 ms |
| 40 B (run 3/3) | 300 | max | 0% | 0 | 0.76 kB/s | 41.46 ms | 81.70 ms |
| 200 B (run 1/3) | 300 | max | 0% | 0 | 1.79 kB/s | 105.08 ms | 147.79 ms |
| 200 B (run 2/3) | 300 | max | 0% | 0 | 1.86 kB/s | 100.10 ms | 139.94 ms |
| 200 B (run 3/3) | 300 | max | 0% | 0 | 1.93 kB/s | 99.65 ms | 132.96 ms |
| 1000 B (run 1/3) | 300 | max | 78.33% | 0 | 0.44 kB/s | 498.83 ms | 818.97 ms |
| 1000 B (run 2/3) | 300 | max | 77.00% | 0 | 0.47 kB/s | 502.21 ms | 799.30 ms |
| 1000 B (run 3/3) | 300 | max | 87.00% | 0 | 0.26 kB/s | 512.92 ms | 819.74 ms |
| 1200 B (run 1/3) | 300 | max | 95.67% | 0 | 0.10 kB/s | 822.76 ms | 860.11 ms |
| 1200 B (run 2/3) | 300 | max | 97.00% | 0 | 0.07 kB/s | 774.58 ms | 861.69 ms |
| 1200 B (run 3/3) | 300 | max | 98.00% | 0 | 0.05 kB/s | 823.38 ms | 881.44 ms |

Comparison vs 2M PHY: 40 B and 200 B stay clean (0% loss) but ~5-8x
slower throughput and ~2x higher RTT, as expected for S=8 coded
symbols. 1000 B and 1200 B are now *consistently* bad on Coded PHY
(1200 B was wildly variable on 2M PHY — 38%/0.33%/0% — but is
consistently 96-98% loss here), because the much lower raw throughput
widens the time window a multi-fragment burst spends exposed to the
same HVN credit-starvation root cause documented above for 2M PHY.

**New finding, differs from 2M PHY:** every 1000 B+ run left the BLE
connection fully dead afterward — zero bytes in either direction,
`crc_errors`/`seq_lost` frozen, not just elevated — confirmed via
`bridge.py`'s own `[1Hz]` stderr rate log, not just linktest's own
loss%. This is unlike the 2M PHY credit-starvation case, which was
confirmed self-recoverable (a clean 40 B burst right after a lossy
1000 B burst came back at 0% loss on the same connection). On Coded
PHY, recovering required killing and restarting both `bridge.py`
processes before the next run; a quick 40 B/10-count probe was used
to confirm link health before and after every large-payload run in
this matrix. Root cause not further isolated this session — plausibly
a connection supervision timeout triggered by Coded PHY's much higher
per-fragment notify latency compounding with repeated write-stall
retries, but that's a guess, not confirmed via SDK source like the 2M
PHY case was.

MinBand's actual payloads (40-70 B deltas) are unaffected: 0% loss on
Coded PHY same as 2M PHY, just slower. The throughput cost (roughly
5-8x versus 2M PHY at small sizes) is the real tradeoff against
whatever range gain Coded PHY buys — no distance test was run this
session (see Gotchas: no RSSI telemetry, no distance-test rig exists
yet).
