# tools

- `link.sh` - macOS dummynet shaping of UDP :7777 (`sudo tools/link.sh set 8Kbit/s 150ms 0.05`).
- `build-ios.sh` - cross-compile the core for iOS and build the XCFramework.
- `eval/` (M7) - offline evaluation. Replays ground-truth logs (synthetic, or `gt-*.csv` from the
  phone) through the WASM `Edge` and `Receiver` in-process over a simulated lossy link, sweeps the
  thresholds, computes the baselines and renders the presentation charts. No server, no sockets.
  Details and methodology: [eval/README.md](eval/README.md).

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
| `runs/eval/baselines.csv` | Baseline A (H.264) and Baseline B (naive metadata) per scenario |
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
| `kind` | `A` (H.264 video) or `B` (naive 30 Hz metadata) |
| `id`, `label`, `resolution` | e.g. `h264_720p`, `H.264 720p`, `1280x720` (A only) |
| `scenario`, `entities_mean`, `entities_max` | B only: per log |
| `bps`, `kbps`, `bytes_per_s` | the bitrate in three units |
| `measured` | `true` once replaced by a phone measurement, `false` for configured numbers |
| `source` | provenance; configured A rows say "configured, to be replaced by measured VideoToolbox numbers" |
