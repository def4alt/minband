# Evaluation findings (synthetic, 2026-10-09)

From `tools/eval` on synthetic ground truth, θ_pos 0.15 m, θ_vel 0.3 m/s, 120 s at 30 Hz, no
budget (unlimited). Rerun with `cd tools/eval && npm run eval`; outputs in `runs/eval/`. Bytes are
on the wire (payload + 28 B UDP/IP per datagram). Protocol v1 (threshold byte, S14).

| Scenario | Entities | B/s | Mean err (cm) | p95 (cm) | vs naive metadata | vs H.264 720p* |
|---|---:|---:|---:|---:|---:|---:|
| static | 3 | 69.4 | 0.0 | 0.0 | 58× | 2,703× |
| one walker | 1 | 127.8 | 3.0 | 11.7 | 17× | 1,468× |
| one walker, 3 cm noise | 1 | 178.4 | 7.3 | 13.0 | 12× | 1,051× |
| three walkers | 3 | 348.3 | 2.6 | 9.3 | 11× | 538× |
| crowd (8) | 8 | 790.0 | 2.8 | 11.7 | 11× | 237× |

\* H.264 is a configured reference (1.5 Mbps), to be replaced by a measured VideoToolbox number.
The error reference is the edge's own tracks, so this measures sync fidelity, not perception.

History: the first version of this table (static 61.5 B/s) predates the 5 s Hello refresh; with it
the same code measured 68.9 / 125.9 / 175.8 / 343.7 / 780.6 B/s. Protocol v1 adds one byte
(`theta_q`) to every Delta and Keyframe: +0.5 to +9.4 B/s, error and availability unchanged at
every loss rate. Without a budget the cadence is the old one (keyframe 2 s, Hello 5 s).

## Protocol findings and decisions

1. **Velocity threshold drives 75–90% of updates** (turns, speed changes), damping only adds
   4–19%. θ_vel = 0.6 cuts bytes ~30% for ~1 cm more error. Decision: keep defaults until real
   phone logs exist, then retune θ_vel relative to θ_pos (candidate 4×) on the noisy logs.
   θ_pos must stay above ~3σ of tracker position noise or deltas explode.
2. **The 3 s age cap never fires** because the 2 s keyframe resets all ghosts first. Keyframes are
   the bandwidth floor (the static room is 100% keyframes). Decision: keep; it is the resync
   mechanism and 60 B/s is negligible. Revisit keyframe period if static scenes matter.
   Revisited with S19 (below): with a slower keyframe the 3 s cap fired for every entity every 3 s
   (97 deltas in 120 s for the static room at 450 bit/s), so the cap is now 1.5 keyframe periods.
3. **A lost Despawn leaves a phantom for up to 10 s** (until receiver gc). Fix in core:
   (a) edge remembers despawned ids per seq and resends the Despawn on nack;
   (b) once all parts of a keyframe are received, the receiver removes entities absent from it.
4. **Repeated nacks double uplink bytes under loss** (crowd 803 → 1493 B/s at 5% loss) because
   `make_ack` re-lists every open gap for 3 s and the server acks every 100 ms. Fix in core:
   a gap is nacked at most once per 500 ms (one round trip plus margin), and the edge ignores
   a repeat nack for an id it repaired within the last 500 ms.
5. **Static objects drop out at 50% loss** (availability 94.7%) because they are refreshed only
   by keyframes and five lost keyframes exceed the 10 s per-entity drop. Fix in core: drop by
   entity age only when the device itself has been silent; while the device is alive,
   keyframe reconciliation (finding 3b) is the removal mechanism.
   Result after the fix: static availability at 50% loss is unchanged (94.3%). The remaining
   misses are start-up (the first delivered Spawn or keyframe takes several seconds at 50% loss)
   plus windows where five keyframes and a Hello are all lost, so the device counts as silent.
   A shorter keyframe period under high loss would close this; not done.
6. **State repair matters for multi-entity scenes** (crowd 5.3 vs 8.0 cm at 20% loss) and is
   nearly free for one entity. Keep.

Items 3–5 are implemented in core (edge.rs, receiver.rs; see DESIGN.md §4 "Nack pacing",
"Despawn repair", "Entity drop rule") and covered by unit tests. The golden file was regenerated.
Rerun `npm run eval` after the next real-link session to confirm phantom duration and uplink
bytes under loss on phone logs.

## Low-rate profiles: cadence from the budget, header and window in the controller (S19, S16, §3.4)

`npm run replay -- runs/synth/<scenario>.csv --budget B --loss L --delay D --ack server` with the
link-box profiles' budgets, loss and one-way delay, plus 1000 bit/s: the per-device share when
eight drones split the hf budget (drones per link). The replay advertises the budget in every ack
like the server, so the edge's cadence and the receiver's coast/stale/drop limits follow it.
Columns: *before* = the code before protocol v1 (controller counted payload only, 2 s keyframes,
5 s Hello, 3 s age cap at every budget); *v1* = cadence, header accounting and paced keyframes
with the fixed 0.5 s controller window; *now* = the budget-scaled window, immediate nacks and the
repair skip (below). Cells: bit/s on the wire before → v1 → now; mean error of present entities
(cm) before → v1 → now. Availability is unchanged within 0.2 points (98.4-99.9 %).

| Profile (budget, loss, delay) | static (3) | one walker | three walkers | crowd (8) |
|---|---|---|---|---|
| telemetry (450, 5 %, 50 ms) | 609 → 97 → **112**; 0 | 472 → 409 → 452; 24.6 → 19.0 → 17.6 | 884 → 634 → 643; 32 → 63 → 59 | 1705 → 1333 → 1356; 28 → 57 → 58 |
| hf x8 share (1000, 1 %, 500 ms) | 559 → 133 → **133**; 0 | 1194 → 416 → **858**; 12.9 → 26.9 → **14.9** | 1255 → 628 → **907**; 31 → 77 → **40** | 1941 → 1252 → 1314; 37 → 76 → 72 |
| lora (1500, 10 %, 300 ms) | 741 → 181 → **181**; 0 | 1900 → 1205 → **1156**; 5.7 → 10.8 → 10.7 | 1263 → 1043 → **1252**; 34 → 39 → **24** | 2304 → 1633 → 1556; 30 → 60 → 60 |
| hf (8000, 1 %, 500 ms) | 559 → 563 → 555; 0 | 2290 → 2316 → 2254; 8.5 | 6663 → 5887 → **6063**; 8.6 → 9.2 → 9.1 | 8838 → 5801 → **6059**; 10.9 → 14.5 → 14.0 |

Bold: within the budget and, for *now*, the best of the three or within 2 cm of it. Notes:

1. **v1 (cadence, header).** The static floor is now the 15 s keyframe and 30 s Hello at 450 bit/s
   (97-112 bit/s) and 6.3 s / 12.7 s at 1500 (181 bit/s), instead of the 2 s keyframe plus 5 s
   Hello at any budget (559-741). The controller sees the 28 B per datagram, so one walker on
   lora and the crowd on hf no longer run 27 % and 10 % over.
2. **The fixed 0.5 s controller window over-throttled below ~2 kbit/s** (v1 column, 1000 bit/s):
   one 68 B datagram in a 0.5 s window reads 1088 bit/s, so every window with a packet widened θ
   (x1.25) and only empty windows narrowed it (x0.9). That balances at ~0.64 datagrams/s whatever
   the budget: 77 % of 450 bit/s but 38 % of 1000 (one walker at 416 bit/s, 27 cm). Eight drones on
   hf delivered 1820 bit/s of 9600 in the e2e test. Now the window holds six one-update datagrams
   at the budget (7.3 s at 450, 3.3 s at 1000, 0.5 s from ~6.5 kbit/s), narrows under 80 %, and
   widens at once when a window's allowance is spent early: 80-92 % of the budget for circling
   walkers at 450-8000 bit/s (unit test), one walker at 1000 bit/s 858 bit/s and 14.9 cm, and eight
   drones on hf deliver 6240 bit/s with 20.5 cm mean twin error in the e2e test. A leaky bucket
   was tried first: it held 87-90 % but its memory made 30 s limit cycles (θ scale 1.7 → 6.5) that
   doubled the telemetry error.
3. **Immediate nacks.** The receiver nacked a gap once it was 200 ms old, but a gap's age only
   advances with later datagrams, so the nack waited for the next datagram after 200 ms (~0.5 s on
   a sparse link, and on the sparsest links the gap was forgotten after 3 s before a second
   datagram came). Gaps are now nacked as soon as they are seen; links here do not reorder.
4. **Repair skip.** With immediate nacks, a lost keyframe on a sparse stream is usually revealed by
   the next keyframe, and the edge then resent what the receiver had just received (lora static
   181 → 248 bit/s). The edge now skips ids that the datagram at the ack's `last_seq` carried.
   Under loss at budget 0 this also trims repeat repairs: static at 20 % loss 80 → 69 B/s, the
   crowd 932 → 920 B/s, errors equal or better (resilience table above).

Three walkers and the crowd still do not fit 450 bit/s, nor the crowd 1000-1500: spawns, despawns
and 8-entity keyframes are not suppressible, as expected (§3.4 item 3); the compact codec (S6) is
the next step there. The replay has no rate limit or queue, so it measures what the edge offers
and the fidelity, not the radio queue: the burst fix (keyframe parts paced one per link time, at
most 2 s of link time each) shows up only on a shaped link (Pi link box, server shaper).

## The "20 % loss" e2e flake (resilience.test.ts)

The test scores the last 10 s of ground truth (4200 rows at 120 Hz) against a 0.2 m mean. The sim's
walker 2 wraps from x = +4 to -4 m and the cup spawns at the same tick every 10 s, in one
datagram, so every scoring window holds one 8 m jump and one spawn. When that datagram is lost
(p = 0.2) the twin is 8 m off and misses the cup until the repair lands; each 100 ms of repair
latency adds ~0.03 m to the window mean, so more than ~0.5 s crosses the bound.

- Pose does not cause it. Pose seqs carry no entities and are never repaired; the extra datagrams
  make gaps visible sooner. Real stack (fresh server and sim per run, the test's procedure): 1/25
  failures with Pose, 3/25 without. A deterministic replay of the e2e timing (WASM edge and
  receiver, sim scene, server ack policy, acks applied per 4-tick sim batch, 1000-3000 seeds):
  8.5 % failures with Pose, 16.9 % without.
- With immediate nacks (note 3) the replay fails 3.7 % with Pose (p90 window mean 0.198 → 0.071
  m), 15.8 % without; with the repair skip (note 4) 4.7 % with Pose. Real stack with Pose: 2/25
  with immediate nacks, 0/25 with both (max 0.182 m, median 0.060 m), so 2/50 after against 4/48
  before (this probe's 1/25 and an earlier 3/23). What remains is the wrap
  datagram and its first repair both lost (~4 %), then the 500 ms re-nack. Fewer redundant
  datagrams (the skip; a stricter seq-keyed repeat rule, tried and dropped) slow the detection of
  the next loss, which is why removing redundancy costs about a point in this test.
- So the remaining failures are statistics of one 8 m jump per window, not a protocol fault. A sim
  walker that does not teleport (or a scoring window that excludes the wrap tick) would make the
  test measure the steady state.
