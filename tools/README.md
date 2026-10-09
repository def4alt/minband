# tools

- `link.sh` - macOS dummynet shaping of UDP :7777 (`sudo tools/link.sh set 8Kbit/s 150ms 0.05`).
- `pi-link.sh` - the Raspberry Pi 5 link box ([HACKATHON_PLAN §3](../docs/HACKATHON_PLAN.md#3-raspberry-pi-5-link-box)):
  hotspot and static Ethernet setup, tc/netem link profiles on UDP :7777, the `contested` loop,
  status, clear. [Below](#pi-5-link-box-pi-linksh).
- `golden-aarch64.sh` - the core's tests, golden vectors included, cross-compiled for aarch64
  Linux and run under qemu-user: the Pi 5's architecture without a Pi. [Below](#golden-vectors-on-aarch64-golden-aarch64sh).
- `test/` - plain-bash tests for `pi-link.sh`.
- `build-ios.sh` - cross-compile the core for iOS and build the XCFramework.
- `eval/` (M7) - offline evaluation. Replays ground-truth logs (synthetic, or `gt-*.csv` from the
  phone) through the WASM `Edge` and `Receiver` in-process over a simulated lossy link, sweeps the
  thresholds, computes the baselines and renders the presentation charts. No server, no sockets.
  Details and methodology: [eval/README.md](eval/README.md).

## Pi 5 link box (pi-link.sh)

Runs on the Pi (Raspberry Pi OS Bookworm, NetworkManager). Phone -> Pi hotspot (`wlan0`) -> Pi ->
Ethernet (`eth0`) -> laptop; the Pi shapes MinBand's UDP port in both directions.

```bash
sudo raspi-config nonint do_wifi_country KR             # once; 5 GHz AP mode needs the country
sudo tools/pi-link.sh setup --password '<8+ chars>'     # hotspot minband-link (5 GHz; --band bg for 2.4)
                                                        # + eth0 192.168.77.1/24; laptop 192.168.77.2/24
sudo tools/pi-link.sh lora        # apply a profile; prints the laptop's budget command, e.g.
                                  #   curl 'http://192.168.77.2:8080/api/budget?bps=1500'
tools/pi-link.sh status           # profile, contested loop, qdiscs and counters
sudo tools/pi-link.sh clear       # back to the default qdiscs
tools/pi-link.sh --dry-run hf     # print the exact tc commands (anywhere, no root)
```

| Profile | netem on UDP :7777, both directions | Edge budget, bit/s |
|---|---|---:|
| `clean` | pass-through (`limit 1000`); same tree, so `status` still counts | 0 (unlimited) |
| `degraded` | `rate 64kbit delay 20ms loss 2% limit 20` | 0 |
| `hf` | `rate 9600bit delay 500ms loss 1% limit 8` | 8000 |
| `lora` | `rate 2kbit delay 300ms loss 10% limit 4` | 1500 |
| `telemetry` | `rate 600bit delay 50ms loss 5% limit 4` | 450 |
| `contested` | `lora`, alternating with random 1-5 s blackouts (background loop, pidfile `/run/minband-pi-link/contested.pid`; any other profile or `clear` stops it) | 1500 |
| `blackout` | `loss 100%` | unchanged |

After each profile the script also prints `curl '.../api/link?profile=external&as=<profile>'`, for
a server that has `/api/link`.

- **Tree.** Per device a `prio` root with 4 bands. The default priomap only uses bands 1:1-1:3, so
  band 1:4 (netem) gets nothing but the u32 filter's matches: UDP dport 7777 on `eth0` egress
  (uplink), UDP sport 7777 on `wlan0` egress (downlink acks). SSH and everything else stay
  unshaped. Switching profiles deletes and re-adds the root qdisc, which does not take the link
  down; the kernel test checks that an open TCP connection survives every switch. The `contested`
  loop only changes the 1:4 leaf (`tc qdisc change`), never the root.
- **Bytes.** Each rate is followed by `-14`, netem's per-packet overhead: at the qdisc a datagram
  still carries its 14 B Ethernet header, so without it netem would count payload + 42 B where the
  server and `tools/eval` count payload + 28 B. `L2_OVERHEAD=0` counts the header.
- **Queue.** netem's `limit` also counts packets waiting out the delay, so it caps datagrams in
  flight. `hf` is the one profile where that binds before the rate: 8 per 500 ms is 16
  datagrams/s, and 9600 bit/s carries 16 datagrams/s of 75 B, so smaller datagrams at a higher
  count (a one-update delta is ~59 B; many devices in the drones-per-link run) are tail-dropped on
  top of the 1 % loss. Raise its `limit` if that run shows drops the rate does not explain.
- **Kernel.** netem needs the `sch_netem` module; Raspberry Pi OS ships it (`modinfo sch_netem`).
- **Settings.** `UP_DEV`, `DOWN_DEV`, `PORT`, `SERVER`, setup's `SSID`/`PASSWORD`/`BAND`/`COUNTRY`,
  `CONTESTED_UP`/`CONTESTED_DOWN` and more: `tools/pi-link.sh --help`.

Tests:

```bash
tools/test/pi-link.test.sh               # dry-run golden: exact tc commands of every profile, both
                                         # directions, budgets, contested, setup, overrides (no root)
sudo tools/test/pi-link-kernel.test.sh   # real kernel, on lo: tc -s counters prove UDP to/from :7777
                                         # lands in 1:4 and other UDP and TCP do not; contested loop
                                         # start/stop, status, clear; TCP survives profile switches
```

The kernel test puts `pfifo` where netem goes (test hook `LEAF_QDISC_OVERRIDE`), and on kernels
without `sch_prio` an `htb` root with the same handles (`ROOT_QDISC_OVERRIDE=htb`): the dev
container's kernel has neither module. On the Pi it uses `prio` and also applies the real `lora`
netem to `lo` and reads it back. So netem itself and `nmcli` are verified only on the Pi; every
netem argument list does parse in iproute2 6.1 (Bookworm's version).

## Golden vectors on aarch64 (golden-aarch64.sh)

```bash
tools/golden-aarch64.sh                  # Linux host: cargo test --target aarch64-unknown-linux-gnu,
tools/golden-aarch64.sh --test golden    # run under qemu-aarch64; extra args go to cargo test
```

Needs `rustup target add aarch64-unknown-linux-gnu` and `apt install gcc-aarch64-linux-gnu
libc6-dev-arm64-cross qemu-user`. Linker and runner come from cargo's
`CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_{LINKER,RUNNER}` variables for that run only; no
`.cargo/config` change. On the Pi itself (any aarch64 Linux) the script just runs `cargo test`,
which is all §4 of the plan asks.

## eval quick start

```bash
cd tools/eval && npm install          # Node >= 23.6 (runs .ts natively); core/pkg-node must be built
npm run eval                          # synth -> sweep -> baselines -> charts, ~15 s
npm run replay -- ../../runs/synth/crowd.csv --theta 0.15 --loss 0.2   # one line per file
npm test
```

Outputs (all under the gitignored `runs/`):

| File | What |
|---|---|
| `runs/synth/<scenario>.csv` | synthetic ground truth, phone format `tick,id,class,x,y,z,vx,vy,vz,conf` |
| `runs/eval/fidelity_vs_bytes.csv` | θ sweep, one row per (scenario, θ_pos) |
| `runs/eval/resilience.csv` | loss sweep, one row per (scenario, loss, repair on/off) |
| `runs/eval/baselines.csv` | Baseline A (H.264), Baseline B (naive metadata) per scenario, Baseline C (AI thumbnail) per scenario and per link profile |
| `runs/eval/fidelity_vs_bytes.svg`, `resilience.svg` | the two charts |
| `runs/eval/summary.md` | key-number table for the slides |

## Columns

Common definitions. *Bytes* are counted at the sender on the uplink (edge -> twin), every
datagram including those the link then drops, as payload + 28 B UDP/IPv4 header. *Twin error* is
evaluated at every logged frame: for each ground-truth row (tick t, entity id) it is the 3D
distance between the logged position and the receiver's `extrapolate_json(t)` position for that
id. A row whose id is absent from the twin is *missing*: it counts as `missing_penalty` (2.0 m) in
the `err_*` columns and is excluded from the `err_*_present` columns. `availability` = share of
ground-truth rows present in the twin. *Phantom* = an entity in the twin that is not in the
ground truth at that tick (e.g. its despawn was lost).

`fidelity_vs_bytes.csv` (loss 0, delay 0, θ_vel = 2·θ_pos; θ_pos = 13 log-spaced points
0.02..2.0 m plus 0.15):

| Column | Meaning |
|---|---|
| `scenario` | log file basename |
| `theta_pos`, `theta_vel` | edge thresholds (m, m/s) |
| `loss`, `delay_ticks` | link settings (0 here) |
| `duration_s`, `frame_hz` | logged span and logged frame rate |
| `entities_mean` | time-averaged tracked entities per frame |
| `datagrams`, `deltas`, `keyframes` | datagrams sent, split by kind (keyframe parts count separately) |
| `updates` | entity updates in all deltas (spawn + update + despawn), from the edge stats |
| `payload_bytes`, `wire_bytes` | sum of datagram sizes; plus 28 B per datagram |
| `bytes_per_s`, `kbps` | `wire_bytes / duration_s`, and the same in kbit/s |
| `err_mean_m`, `err_p95_m`, `err_max_m` | twin error with missing rows at the penalty |
| `err_mean_present_m` | mean twin error over present rows only |
| `availability`, `missing_rows`, `phantom_rows`, `gt_rows` | see above; `gt_rows` = rows evaluated |

`resilience.csv` (θ_pos 0.15, θ_vel 0.3, loss 0 / 5 / 20 / 50 % applied independently to both
directions, 6 ticks = 50 ms one-way delay, 10 seeds averaged for loss > 0):

| Column | Meaning |
|---|---|
| `repair`, `ack_mode` | `on`/`gap`: every 12 ticks, if `needs_ack()`, the receiver's ack (with nacks) goes back to the edge, which resends current state (state repair). `off`/`none`: no acks after the initial pre-ack, recovery only via 2 s keyframes. `server`/`server`: the live server's cadence, an ack after each delivered datagram when `needs_ack()` or 100 ms since the last ack (not charted; in summary.md) |
| `seeds` | runs averaged (1 for loss 0, which is deterministic) |
| `bytes_per_s`, `kbps`, `datagrams`, `updates` | as above, mean over seeds |
| `lost_datagrams` | uplink datagrams dropped by the simulated link |
| `acks_sent` | acks the receiver produced (before reverse-link loss) |
| `gaps_detected`, `nacks_sent` | receiver stats |
| `err_mean_m`, `err_p95_m`, `err_mean_present_m`, `err_p95_present_m` | mean over seeds |
| `err_max_m`, `phantom_max_s` | max over seeds; `phantom_max_s` = longest single phantom episode |
| `availability`, `missing_rows`, `phantom_rows`, `gt_rows` | mean over seeds |

`baselines.csv`:

| Column | Meaning |
|---|---|
| `kind` | `A` (H.264 video), `B` (naive 30 Hz metadata) or `C` (AI thumbnail) |
| `id`, `label`, `resolution` | e.g. `h264_720p`, `H.264 720p`, `1280x720` (A only); C: `thumb_equal_bytes`, or `thumb_hf` / `thumb_lora` / `thumb_telemetry` |
| `scenario` | B: per log. C: the log for `thumb_equal_bytes`, empty for the link rows |
| `entities_mean`, `entities_max` | B only |
| `bps`, `kbps`, `bytes_per_s` | the bitrate in three units; C: the rate the thumbnail feed gets (MinBand's wire rate at the default θ_pos, or the link profile's rate) |
| `chip_bytes`, `interval_s` | C only: chip size (150 B) and N = (`chip_bytes` + 28) × 8 / `bps`, seconds between chips |
| `measured` | `true` once replaced by a phone measurement, `false` for configured or computed numbers |
| `source` | provenance; configured A rows say "configured, to be replaced by measured VideoToolbox numbers" |
