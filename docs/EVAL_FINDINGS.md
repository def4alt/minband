# Evaluation findings (synthetic, 2026-10-09)

From `tools/eval` on synthetic ground truth, θ_pos 0.15 m, θ_vel 0.3 m/s, 120 s at 30 Hz.
Rerun with `cd tools/eval && npm run eval`; outputs in `runs/eval/`.

| Scenario | Entities | B/s | Mean err (cm) | p95 (cm) | vs naive metadata | vs H.264 720p* |
|---|---:|---:|---:|---:|---:|---:|
| static | 3 | 61.5 | 0.0 | 0.0 | 65× | 3,050× |
| one walker | 1 | 118.5 | 3.0 | 11.7 | 18× | 1,582× |
| one walker, 3 cm noise | 1 | 168.4 | 7.3 | 13.0 | 13× | 1,114× |
| three walkers | 3 | 336.3 | 2.6 | 9.3 | 12× | 558× |
| crowd (8) | 8 | 773.2 | 2.8 | 11.7 | 11× | 242× |

\* H.264 is a configured reference (1.5 Mbps), to be replaced by a measured VideoToolbox number.
The error reference is the edge's own tracks, so this measures sync fidelity, not perception.

## Protocol findings and decisions

1. **Velocity threshold drives 75–90% of updates** (turns, speed changes), damping only adds
   4–19%. θ_vel = 0.6 cuts bytes ~30% for ~1 cm more error. Decision: keep defaults until real
   phone logs exist, then retune θ_vel relative to θ_pos (candidate 4×) on the noisy logs.
   θ_pos must stay above ~3σ of tracker position noise or deltas explode.
2. **The 3 s age cap never fires** because the 2 s keyframe resets all ghosts first. Keyframes are
   the bandwidth floor (the static room is 100% keyframes). Decision: keep; it is the resync
   mechanism and 60 B/s is negligible. Revisit keyframe period if static scenes matter.
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
