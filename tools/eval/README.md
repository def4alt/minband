# tools/eval - offline evaluation (M7)

Produces the presentation's numbers and charts from ground-truth logs, fully offline: every log
is replayed in-process through the real WASM `Edge` and `Receiver` (`core/pkg-node`, imported as
`minband-core`) over a simulated link. It never starts the server or binds a port. Column
definitions for every CSV are in [../README.md](../README.md#columns).

Requires Node >= 23.6 (TypeScript runs natively, no build step) and a built `core/pkg-node`
(`wasm-pack build --target nodejs --out-dir pkg-node --features wasm` in `core/`).

```bash
npm install
npm run eval                      # synth -> sweep -> baselines -> charts (~15 s)
npm test                          # CSV round trip, replay sanity, baseline formula
npm run typecheck
```

| Script | What |
|---|---|
| `npm run synth -- [--scenario crowd] [--duration 120] [--hz 30] [--noise 0.03] [--seed 1]` | synthetic ground truth into `runs/synth/` |
| `npm run replay -- <gt.csv>... [--theta 0.15] [--theta-vel 0.3] [--loss 0.2] [--ack-loss 0.2] [--delay 6] [--ack gap\|server\|none] [--seed 1] [--penalty 2.0] [--json]` | one replay, one line per file |
| `npm run sweep -- [--gt <csv>]...` | θ sweep and loss sweep -> `fidelity_vs_bytes.csv`, `resilience.csv` |
| `npm run baselines -- [--gt <csv>]... [--baseline-a <json>]` | `baselines.csv` |
| `npm run charts` | `fidelity_vs_bytes.svg`, `resilience.svg`, `summary.md` from the CSVs |
| `npm run ablation -- <gt.csv> [--theta 0.15] [--damping 1.0,0.85]` | analysis only: how many updates the person damping prior costs (TS mirror of the trigger, validated against the core) |

Paths given to `npm run ...` resolve against the directory you ran npm from. Outputs go to
`runs/` at the repo root (override with `MINBAND_RUNS=/some/dir`).

## Real phone logs

Export `gt-<unix>.csv` from the phone (Files app), put it under `runs/phone/`, then

```bash
npm run eval -- --gt ../../runs/phone/gt-1712345.csv --gt ../../runs/phone/gt-1712399.csv
```

skips the synthetic step and evaluates those logs (scenario name = file basename). The phone logs
one row per track per ARKit frame (60 Hz, so a 2-tick frame step) and nothing when it sees
nothing; the replay infers the frame step from the median tick spacing and ticks the edge with
an empty track list across gaps so despawns happen as they did live.

## Synthetic scenarios

Motion is integrated at 120 Hz and logged at 30 Hz (the tracker rate in DESIGN 3.1); velocities
are exact derivatives, i.e. a perfect tracker. Same seed, same bytes.

| Scenario | Content |
|---|---|
| `static` | chair, tv, laptop at rest |
| `one_walker` | one person on a 5-waypoint room tour at 1.2 m/s, bounded turn rate and acceleration, one 2 s stop |
| `one_walker_noisy` | the same trajectory plus gaussian observation noise: 3 cm on position, 6 cm/s on velocity |
| `three_walkers` | the tour + a 2 m-radius circular loop at 1 m/s + a stop-and-go walker (1.5 s stops every 2.3 m) |
| `crowd` | 8 concurrent people, random waypoints at 0.8-1.5 m/s with random pauses; each leaves after 25-60 s and a newcomer (new id) enters at the room edge |

With noise, the logged (noisy) track *is* the reference, exactly as with a real phone log where
the tracker output is all we have. The error floor at small θ is therefore ~0 (the twin copies
the noise at full rate), and at large θ the error includes the noise.

## Link model and metrics

Per tick from the first to the last logged tick: acks due are delivered to the edge; on a logged
frame `edge.tick(tracks, tick)` runs and each datagram is counted on the wire, then dropped with
probability `loss` (seeded Bernoulli) or queued for `tick + delay`; due datagrams are delivered
to the receiver; on a logged frame every ground-truth row is compared with
`extrapolate_json(tick)` (edge ticks, so no clock offset is needed offline); every 12 ticks the
receiver's ack goes back over the same lossy, delayed link when `needs_ack()` (mode `gap`, as in
the golden test); mode `server` instead acks after each delivered datagram when `needs_ack()` or
100 ms have passed since the last ack (what `server/src/world.ts` does); mode `none` sends no
acks (no state repair). The edge is pre-acked with `[0,4,0,0,0]` so it skips the Hello handshake.

The fidelity sweep uses loss 0, delay 0, θ_vel = 2·θ_pos. With that link the twin error at logged
frames can never exceed θ_pos (the edge checks exactly that prediction), which the tests assert.
The resilience sweep uses θ_pos 0.15, 6 ticks (50 ms) one-way delay and 10 seeds per point, for
ack modes `gap` (repair on), `none` (repair off) and `server`.

## Baselines

**Baseline B** (naive metadata) is computed from each log: `entities * 31 B * 30 Hz * 8 + 30 Hz *
40 B * 8` bits/s with the time-averaged entity count, the same formula the server shows live.

**Baseline A** (H.264) is, until measured, a table of *configured* reference bitrates: 720p 1.5
Mbps, 480p 500 kbps, 360p 250 kbps, labelled "configured, to be replaced by measured
VideoToolbox numbers" in every CSV, chart and table. The measured number comes from the phone:
during the same recorded session that writes `gt-*.csv`, the app also feeds each
`ARFrame.capturedImage` (scaled to 1280x720, 854x480 and 640x360) into an `AVAssetWriter` with
`AVVideoCodecType.h264` (hardware VideoToolbox encoder), `AVVideoAverageBitRateKey` set to the
target bitrate, `AVVideoExpectedSourceFrameRateKey` 30, `AVVideoMaxKeyFrameIntervalKey` 60 and
frame reordering off (a live downlink cannot use B-frames), timestamps from `ARFrame.timestamp`.
Rate control over- or under-shoots the target depending on scene content, which is why the
number is measured, not quoted: measured bps = encoded video track bytes * 8 / session duration
(the file size of the .mp4 minus container overhead, or the sum of sample sizes read back with
`AVAssetReader`). To plug it in, write `runs/baseline_a.json`:

```json
{ "entries": [
  { "id": "h264_720p", "bps": 1432112, "source": "measured: AVAssetWriter H.264 720p30 target 1.5 Mbps, session gt-1712345" },
  { "id": "h264_480p", "bps": 487000,  "source": "measured: ... target 500 kbps, session gt-1712345" }
] }
```

and re-run `npm run baselines && npm run charts` (or `npm run eval`). Entries replace the
configured rows with the same `id` (unknown ids are added as extra reference lines), flip
`measured` to `true` in `baselines.csv`, and drop the "(configured)" label from the chart and
`summary.md`. `--baseline-a <file>` reads a different file.
