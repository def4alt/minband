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

## Low-rate profiles: cadence from the budget, header in the controller (S19, S16, §3.4)

`npm run replay -- runs/synth/<scenario>.csv --budget B --loss L --delay D --ack server` with the
link-box profiles' budgets, loss and one-way delay. The replay now advertises the budget in every
ack like the server, so the edge's cadence and the receiver's coast/stale/drop limits follow it.
Before = the code before these changes (controller counted payload only, 2 s keyframes, 5 s Hello,
3 s age cap at every budget). Cells: bit/s on the wire before → after; mean error of present
entities (cm) before → after. Availability is unchanged within 0.1 points (98.5-99.9 %).

| Profile (budget, loss, delay) | static (3) | one walker | three walkers | crowd (8) |
|---|---|---|---|---|
| telemetry (450, 5 %, 50 ms) | 609 → **97**; 0 → 0 | 472 → **409**; 24.6 → 19.0 | 884 → 634; 32 → 63 | 1705 → 1333; 28 → 57 |
| lora (1500, 10 %, 300 ms) | 741 → **181**; 0 → 0 | 1900 → **1205**; 5.7 → 10.8 | 1263 → **1043**; 34 → 39 | 2304 → 1633; 30 → 60 |
| hf (8000, 1 %, 500 ms) | 559 → 563; 0 → 0 | 2290 → 2316; 8.5 → 8.5 | 6663 → 5887; 8.6 → 9.2 | 8838 → **5801**; 10.9 → 14.5 |

Bold: now within the budget. Before, the static room alone exceeded the telemetry budget, one
walker on lora and the crowd on hf ran 27 % and 10 % over (the controller did not see the 28 B per
datagram), and the static floor was the 2 s keyframe plus the 5 s Hello at any budget. Now it is
97 bit/s at 450 (keyframe every 15 s, Hello 30 s) and 181 bit/s at 1500 (6.3 s, 12.7 s). Where the
controller now holds the budget it does so by widening θ, so the error goes up (one walker on
lora, crowd on hf). Three walkers and the crowd still do not fit 450 bit/s, nor the crowd 1500:
spawns, despawns and 8-entity keyframes are not suppressible, as expected (§3.4 item 3); the
compact codec (S6) is the next step there.

The replay has no rate limit or queue, so it measures what the edge offers and the fidelity, not
the radio queue: the burst fix (keyframe parts paced one per link time, at most 2 s of link time
each) shows up only on a shaped link (Pi link box, server shaper).
