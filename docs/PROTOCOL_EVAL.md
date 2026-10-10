# Protocol evaluation: harness, metrics, what changed

Status: 2026-10-10, branch `feature/protocol-v2`. Companion to `proto/PROTOCOL.md` (the wire
format and scheduling rules) and `docs/DESIGN.md` §9 (the metrics this doc implements).

## 1. The harness

`tools/sidebyside/scripts/eval.mjs` replays a footage `tracks.csv` through the **real WASM edge
and receiver** (`core/pkg-node`) at 10 Hz steps, faster than real time (a 165 s replay of the busy
clip takes about 3 s). Between them sits a shaper: the profile's budget is the edge's budget (and
the receiver's), a one-way delay, random loss on both directions, scripted blackouts (both
directions down), and an optional uplink loss or no uplink at all. Loss is driven by a seeded
PRNG, so a before/after pair sees the same drops.

```
node tools/sidebyside/scripts/eval.mjs --clip cons2 --profile lora --blackout 30:60
node tools/sidebyside/scripts/eval.mjs --clip all --profile lora,telemetry --blackout 15:20,15:60 --json out.json
node tools/sidebyside/scripts/eval.mjs --clip busy --profile lora --focus auto --debug
```

| flag | default | meaning |
|---|---|---|
| `--clip cons2\|busy\|all\|<dir>` | cons2 | `cons2` = `runs/footage/meva-uav-0307-1720/cons2` (35 tracks, 8-35 s, about 2 live contacts); `busy` = `runs/footage/meva-2018-03-13.16-00-14-bf` (206 tracks, 80 entities per frame, about 17 live contacts after grouping) |
| `--profile a,b\|all` | lora | the driver's profiles: clean (unlimited), hf 9600, lora 2000 / 10 % / 0.3 s, telemetry 600 / 5 % / 0.05 s, contested (lora + 1-5 s bursts every 3-8 s), blackout, dynamic (11) |
| `--blackout s:len[,s:len]` | none | scripted blackouts |
| `--budget`, `--loss`, `--delay`, `--uplink-loss`, `--no-uplink` | profile | overrides |
| `--duration` | clip end + 75 s | long enough for the 60 s depart timer and the tombstone ladder |
| `--seed` | 1 | loss and nonce |
| `--focus auto\|<id>` | off | runs the same replay twice, once with one contact focused (track mode, renewed every 5 s), and reports the delta |
| `--dev-factor k` | 1 | experiment knob: the edge's position change threshold becomes `k x max(ce, 2 pos_res)` |
| `--level n` | adaptive | pin the detail level (PROTOCOL.md 6.4); 1 = grouping and thresholds as before adaptation |
| `--ladder '<json>'`, `--coarsen-s s` | built in, 2 | replace the detail ladder; the backlog that coarsens |
| `--debug` | | per-state honesty breakdown, revision reasons, unmatched transitions |
| `--json path` | | every number, plus a 1 Hz series of completeness |

The drone's own state is what the driver uses (camera nadir from `summary.json`, the fitted height
and pitch); the origin is the Muscatatuck site. Tracks: `e = x`, `n = -z`.

## 2. Metrics, as computed

Every 100 ms step compares the edge's snapshot (truth: its contacts, revisions, states) with the
receiver's snapshot at the same edge tick (dead-reckoned, with `ce_shown` and liveness).

| metric | definition in the harness |
|---|---|
| **comp@rev** (picture completeness) | fraction of the edge's live, non-lost, top-level contacts the receiver holds at the edge's *current* revision; a revision the edge bumped but has not emitted yet counts against it. Mean over steps with at least one live contact |
| **comp@any** | the same, at any revision (the receiver knows the contact exists) |
| **event latency** | the harness diffs the edge snapshot step to step and logs transitions (new, confirmed, moving, stopped, static, lost, reacquired, departed, grew, shrank) with the step time; each receiver derived event (`events_json`) is matched to the oldest pending edge transition of the same id and kind; latency = receiver arrival step - edge transition step (so the link delay is included). Median / p90 per kind; **miss** = edge transitions never derived (mostly *collapsed* ones: three `grew` in 2 s arrive as one record, a `static` that turned `lost` before its first copy) |
| **recovery** | after a scripted blackout, seconds from its end until comp@rev first reaches 0.95 |
| **honesty** | over all (step, held contact) pairs where both ends have the contact live: fraction whose edge centroid lies within `ce_shown` of the receiver's dead-reckoned position (target >= 0.68); `err m` is the mean distance, `ce_shown m` the mean radius shown (the cost of honesty) |
| **k/n** | mean of `known / Ego.n_contacts` as the receiver would display it |
| **bytes** | B/s emitted by the edge by record type (TLV included), frame headers, carrier overhead (28 B UDP/IP per frame); delivered B/s separately; uplink B/s |
| **focus cost** | B/s with one focused contact minus without, and that contact's own comp@rev, error and honesty |
| **level, changes** | the edge's detail level over the replay (time mean, range), how many times it changed, and flaps (a change undone within 30 s) |
| **trk shown, trk err** | every 0.5 s, for each footage track an edge contact holds: whether the receiver shows that contact, and the distance from the track to the receiver's centroid (median / p90). The cost of grouping, next to completeness |

## 3. Before and after

Both tables: seed 1, duration clip + 75 s, the profile's loss and delay, uplink on. Blackout rows
are separate runs (`--blackout 10:20` on cons2; `15:20` and `15:60` on busy). Latency cells are
median/p90 seconds, `(n miss)` the transitions never derived.

### Before (commit c07c2b6 core)

| clip | profile | bit/s | loss | comp@rev | comp@any | honest | err m | ce_shown m | k/n | new s med/p90 | moving s | lost s | departed s | app B/s | wire B/s | contact B/s | recovery |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cons2 | clean | 0 | 0 | 95.8% | 98.7% | 99.7% | 0.9 | 5.3 | 98.7% | 0.1/0.1 | 0.1/0.1 | 0.1/0.1 | 0.1/0.1 | 488.0 | 768.0 | 115.2 | - |
| cons2 | hf | 9600 | 0.01 | 81.6% | 93.5% | 98.3% | 1.2 | 5.3 | 96.9% | 0.5/0.5 | 0.5/0.6 | 0.5/0.5 | 0.5/0.5 | 212.5 | 330.6 | 113.7 | - |
| cons2 | lora | 2000 | 0.1 | 85.0% | 95.3% | 98.8% | 1.1 | 5.2 | 95.2% | 0.4/0.5 | 0.4/0.5 | 0.3/0.6 | 0.4/2.7 | 110.9 | 173.7 | 72.4 | - |
| cons2 | telemetry | 600 | 0.05 | 50.1% | 78.0% | 88.1% | 2.2 | 5.2 | 95.4% | 2.8/3.8 | 1.4/3.4 | 2.6/9.3 | 1.3/3.9 | 44.3 | 69.3 | 30.1 | - |
| cons2 | contested | 2000 | 0.1 | 55.7% | 80.5% | 94.0% | 1.4 | 4.8 | 90.9% | 1.3/3.4 | 0.6/2.9 (2 miss) | 1.1/2.8 | 0.3/2.9 | 116.0 | 181.1 | 76.7 | - |
| busy | clean | 0 | 0 | 93.4% | 99.3% | 99.6% | 1.3 | 5.3 | 98.7% | 0.1/0.1 | 0.1/0.1 | 0.1/0.1 | 0.1/0.1 | 1888.0 | 2168.0 | 1515.2 | - |
| busy | hf | 9600 | 0.01 | 62.6% | 94.3% | 95.7% | 1.7 | 5.7 | 98.4% | 0.8/1.5 | 0.9/1.4 (3 miss) | 1.0/1.3 (1 miss) | 0.7/1.2 | 1019.1 | 1094.3 | 942.0 | - |
| busy | lora | 2000 | 0.1 | 28.3% | 72.5% | 86.9% | 3.1 | 5.8 | 88.6% | 5.4/8.8 (15 miss) | 5.4/8.9 (30 miss) | 5.4/17.3 (6 miss) | 4.1/8.1 | 223.3 | 249.5 | 199.0 | - |
| busy | telemetry | 600 | 0.05 | 6.8% | 37.6% | 70.2% | 5.0 | 9.6 | 65.6% | 32.8/62.3 (67 miss) | 32.8/59.7 (74 miss) | 28.2/50.0 (34 miss) | 30.1/57.0 (46 miss) | 51.0 | 74.5 | 37.6 | - |
| busy | contested | 2000 | 0.1 | 21.0% | 64.0% | 86.2% | 6.2 | 19.8 | 92.8% | 6.8/19.8 (29 miss) | 6.7/20.8 (41 miss) | 8.9/27.3 (11 miss) | 6.7/19.1 (8 miss) | 223.2 | 249.3 | 198.8 | - |
| cons2 | lora | 2000 | 0.1 | 29.5% | 37.6% | 98.0% | 2.1 | 5.8 | 95.1% | 7.6/23.0 (1 miss) | 1.7/17.1 (4 miss) | 0.3/13.5 | 0.3/12.8 | 115.6 | 177.2 | 77.8 | 20s: 4.0s |
| cons2 | telemetry | 600 | 0.05 | 15.6% | 28.4% | 70.9% | 3.6 | 4.3 | 93.6% | 12.0/26.7 (2 miss) | 26.0/26.0 (5 miss) | 5.5/21.2 | 1.3/9.8 | 43.1 | 65.8 | 29.7 | 20s: 9.4s |
| busy | lora | 2000 | 0.1 | 22.1% | 62.5% | 83.7% | 6.1 | 14.0 | 82.3% | 8.1/20.0 (27 miss) | 8.1/20.6 (43 miss) | 6.7/15.4 (6 miss) | 4.9/11.5 | 223.1 | 249.3 | 198.8 | 20s: never (max 47.8%) |
| busy | telemetry | 600 | 0.05 | 3.2% | 30.3% | 64.5% | 10.2 | 15.5 | 55.0% | 43.9/67.0 (79 miss) | 38.6/61.4 (83 miss) | 34.6/52.2 (44 miss) | 35.3/62.9 (48 miss) | 50.9 | 74.5 | 37.5 | 20s: never (max 10.3%) |
| busy | lora | 2000 | 0.1 | 12.6% | 48.8% | 74.5% | 15.1 | 74.7 | 80.5% | 11.0/55.2 (48 miss) | 11.7/55.2 (62 miss) | 16.5/44.6 (21 miss) | 5.1/29.5 (15 miss) | 222.4 | 248.6 | 197.9 | 60s: never (max 36.4%) |
| busy | telemetry | 600 | 0.05 | 1.8% | 24.5% | 64.3% | 14.7 | 70.6 | 43.2% | 56.3/73.7 (99 miss) | 59.3/79.2 (100 miss) | 44.9/54.6 (62 miss) | 49.8/99.6 (50 miss) | 50.9 | 74.5 | 37.5 | 60s: never (max 0.0%) |

### After (this change set)

| clip | profile | bit/s | loss | comp@rev | comp@any | honest | err m | ce_shown m | k/n | new s med/p90 | moving s | lost s | departed s | app B/s | wire B/s | contact B/s | recovery |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cons2 | clean | 0 | 0 | 95.8% | 98.7% | 99.7% | 0.9 | 6.7 | 98.7% | 0.1/0.1 | 0.1/0.1 | 0.1/0.1 | 0.1/0.1 | 488.0 | 768.0 | 115.2 | - |
| cons2 | hf | 9600 | 0.01 | 81.6% | 93.5% | 98.3% | 1.2 | 8.3 | 96.9% | 0.5/0.5 | 0.5/0.6 | 0.5/0.5 | 0.5/0.5 | 212.5 | 330.6 | 113.7 | - |
| cons2 | lora | 2000 | 0.1 | 85.0% | 95.3% | 98.8% | 1.1 | 6.2 | 95.2% | 0.4/0.5 | 0.4/0.5 | 0.3/0.6 | 0.4/2.7 | 110.9 | 173.7 | 72.4 | - |
| cons2 | telemetry | 600 | 0.05 | 52.0% | 79.2% | 92.3% | 2.0 | 6.6 | 98.5% | 2.6/3.8 | 1.4/3.4 | 2.6/11.8 | 1.3/3.3 | 42.8 | 66.8 | 29.0 | - |
| cons2 | contested | 2000 | 0.1 | 55.7% | 80.5% | 94.0% | 1.4 | 6.1 | 90.9% | 1.3/3.4 | 0.6/2.9 (2 miss) | 1.1/2.8 | 0.3/2.9 | 116.0 | 181.1 | 76.7 | - |
| busy | clean | 0 | 0 | 93.4% | 99.3% | 99.7% | 1.3 | 5.7 | 98.7% | 0.1/0.1 | 0.1/0.1 | 0.1/0.1 | 0.1/0.1 | 1888.0 | 2168.0 | 1515.2 | - |
| busy | hf | 9600 | 0.01 | 63.6% | 95.0% | 96.7% | 1.7 | 6.2 | 99.2% | 0.8/1.1 | 0.8/1.2 (2 miss) | 1.0/1.4 (1 miss) | 0.7/1.2 | 1023.7 | 1095.5 | 948.9 | - |
| busy | lora | 2000 | 0.1 | 29.7% | 74.5% | 90.7% | 3.5 | 7.5 | 88.0% | 5.2/7.6 (11 miss) | 5.0/7.6 (23 miss) | 4.8/8.0 (5 miss) | 3.7/6.9 (4 miss) | 222.8 | 248.8 | 198.5 | - |
| busy | telemetry | 600 | 0.05 | 7.6% | 38.5% | 84.3% | 4.4 | 9.5 | 63.6% | 34.1/56.9 (61 miss) | 35.1/56.5 (70 miss) | 28.4/48.4 (30 miss) | 25.3/52.9 (39 miss) | 51.3 | 74.9 | 37.9 | - |
| busy | contested | 2000 | 0.1 | 15.5% | 66.9% | 90.7% | 5.0 | 41.2 | 98.7% | 6.3/16.3 (24 miss) | 6.1/16.3 (37 miss) | 6.2/23.4 (11 miss) | 4.9/17.7 (23 miss) | 223.1 | 249.1 | 198.8 | - |
| cons2 | lora | 2000 | 0.1 | 29.5% | 37.6% | 98.0% | 2.1 | 7.2 | 95.1% | 7.6/23.0 (1 miss) | 1.7/17.1 (4 miss) | 0.3/13.5 | 0.3/12.8 | 115.6 | 177.2 | 77.8 | 20s: 4.0s |
| cons2 | telemetry | 600 | 0.05 | 18.2% | 26.0% | 94.2% | 3.5 | 8.5 | 96.8% | 7.8/35.3 (1 miss) | 3.4/15.5 (4 miss) | 2.7/25.8 | 1.2/25.5 | 43.5 | 65.9 | 30.2 | 20s: never (max 80.0%) |
| busy | lora | 2000 | 0.1 | 25.3% | 67.1% | 91.2% | 4.2 | 12.7 | 92.9% | 6.5/17.5 (21 miss) | 6.6/19.0 (32 miss) | 5.9/12.8 (9 miss) | 4.4/7.8 (11 miss) | 223.0 | 248.9 | 198.5 | 20s: never (max 53.6%) |
| busy | telemetry | 600 | 0.05 | 4.5% | 30.1% | 86.9% | 9.5 | 23.0 | 50.9% | 45.7/61.4 (70 miss) | 46.7/61.4 (77 miss) | 36.8/50.4 (34 miss) | 29.5/54.8 (43 miss) | 51.3 | 74.9 | 37.9 | 20s: never (max 14.3%) |
| busy | lora | 2000 | 0.1 | 13.9% | 49.2% | 89.7% | 14.1 | 72.2 | 87.9% | 8.6/56.0 (51 miss) | 9.2/57.6 (56 miss) | 8.9/48.0 (28 miss) | 4.6/20.3 (27 miss) | 222.7 | 248.6 | 198.4 | 60s: never (max 42.9%) |
| busy | telemetry | 600 | 0.05 | 2.7% | 25.1% | 85.1% | 13.6 | 78.3 | 42.0% | 51.7/74.3 (91 miss) | 51.7/76.8 (91 miss) | 40.0/50.7 (56 miss) | 35.6/72.4 (52 miss) | 51.3 | 74.9 | 37.9 | 60s: never (max 14.3%) |

Focus cost (after, `--focus auto`, the contact holding the longest track, track mode): cons2/lora
-2 B/s (the focused contact's 1 s repeats replace ladder copies), cons2/telemetry +2.6 B/s,
busy/lora and busy/telemetry +0 B/s (the link is saturated; focus takes rank 0 out of the same
budget); the focused contact itself sits at 97-98 % comp@rev with 0.3 m error on the busy clip
and 74-75 % on cons2 (it is a 1 s focus interval against a 10 Hz truth and a 0.3 s link).

Simplex (`--no-uplink`, lora): cons2 82.4 / 94.1 % at 130 B/s (no digest suppression: +17 %
bytes), busy 28.9 / 74.0 %.

## 4. What changed and why

1. **Frame builder order: ladder step before overdue** (`core/src/edge.rs`, `build_frame`;
   PROTOCOL.md §6.3). The candidate key was `(class rank, -overdue)`; it is now `(class rank,
   ladder step, -overdue)`. Under saturation the old key sent the second and third copies of old
   revisions before the first copy of a newborn whose `due` was set to its birth tick (the
   "newborn starts at due = 0" family of bugs). A first copy reaches the receiver with probability
   `1 - p`, a k-th copy adds `p^k`, so the step is the right primary key. Test:
   `edge::tests::fresh_revisions_go_before_repeats` (six contacts due for a second copy with
   100 ticks of overdue, a newborn due for its first with 10; a 100 B frame must carry the
   newborn). Effect: small on these clips (busy/lora comp@rev 28.3 -> 29.7 %, new-contact p90
   8.8 -> 7.6 s, departed misses on busy 4 -> 4) because the busy clip is dominated by fresh
   revisions anyway (see §5); no change where the link is not saturated. Kept because it is
   correct by construction and costs nothing.
2. **`ce_shown` growth per motion state** (`core/src/receiver.rs`, `snapshot`; PROTOCOL.md §5.3).
   The flat rule (static never grows; stopped/unknown grow only at the class cap after 30f s) was
   dishonest exactly where the link is late: with `--debug` the outside-the-circle pairs were
   stale `static` (59-72 % inside), stale `stopped` (30-43 %) and stale `unknown` (15-38 %)
   contacts whose revision was in the edge's queue. New rule: static creeps at the class `lo`
   threshold, capped at one more `ce` (the drift the edge tolerates before revising);
   stopped/unknown grow at the class `hi` threshold at once and at the cap once overdue; moving
   unchanged. Tests: `receiver::tests::dead_reckoning_and_the_growing_radius` (vehicle static:
   6 -> 9 m at 10 s, 12 m at 100 s; stopped dismount 11 m at 10 s, 56 m at 40 s). Effect:
   honesty busy/telemetry 70.2 -> 84.3 %, busy/lora 86.9 -> 90.7 %, cons2/telemetry 88.1 ->
   92.3 %, contested 86.2 -> 90.7 %; busy/telemetry with a 20 s blackout 64.5 -> 86.9 %, 60 s
   64.3 -> 85.1 %. Cost: mean `ce_shown` +1 to +2 m on the thin profiles (5.8 -> 7.5 m on
   busy/lora), +3 m on hf (longer silences at the 10 s floor). Every profile is now >= 84 %.
3. **Revision reason counters** (`core/src/contacts.rs`, `ContactStats.rev_why`, exposed in
   `EdgeSnapshot.stats`): first send / state or mix / position / ce / course / speed. Diagnostic
   only; the harness prints it. On busy/lora: 1131 revisions in 165 s, 754 by position deviation,
   278 by state or mix, 43 course, 56 speed; the edge sent 1267 records, so almost every send was
   a fresh revision and only 1 stale copy ever reached the receiver.
4. **`ContactConfig.dev_factor`** (default 1.0, i.e. the spec's `max(ce, 2 pos_res)`), and
   `#[serde(default)]` on `ContactConfig` so the WASM config can set a subset. Experiment knob,
   see §5.

Tests: 23 -> 24 (`cargo test`), all green. No wire-format change; §7 sizes and §8 guarantees of the
spec are untouched. The WASM package was rebuilt (`core/pkg-node`).

## 5. Tried and dropped

- **Wider position change threshold** (`--dev-factor 1.5` and `2.0`): busy/lora comp@rev 29.7 ->
  32.5 / 33.2 %, contact bytes 198 -> 193 B/s at 2.0, cons2 unchanged, telemetry unchanged,
  honesty unchanged (the moving-circle growth covers the extra slack). Marginal, and it trades
  the spec's "wrong by more than its own radius" rule for 3 points; left at 1.0.
- **Smaller frames under loss**: not run, by arithmetic. On UDP the carrier costs 28 B per frame
  against the budget; at 250 B/s one frame per second already spends 38 B (15 %) on headers,
  four per second would spend 60 %. The target of one second of link time stays. On a serial
  carrier (0 B overhead) the question is open, see §6.
- **Ladder spacing and floor**: not changed. On cons2 the ladder is not the limit (lora comp@rev
  85 %, the gap is the 0.1 s step quantisation plus the 0.3 s link: clean is 95.8 % for the same
  reason); on busy the link is oversubscribed and repeats never get a turn, so the ladder
  constants do not matter there either.
- **Digest cadence**: 5 s digests deliver 90 % on lora and suppress 17 % of the bytes
  (simplex run above); busy/lora is unaffected because nothing lives long enough to be acked.

## 6. Open issues

- **The busy clip at 600 bit/s is oversubscribed, not mis-scheduled.** 17 live contacts after
  grouping, churning (the tracker re-ids; contacts are born and lost every few seconds), at
  38 B/s of contact bytes: comp@any 38 %, new-contact median latency 34 s, k/n 64 %. The honest
  readout works (k of n shows it) but the picture does not. Remedies are above the protocol:
  coarser grouping at the floor regime (larger `link_m` when `f` is large), suppressing
  unconfirmed or short-lived contacts entirely below some budget, or a per-contact value
  (moving > static, groups > singles) in the rank.
- **Recovery after a blackout on a saturated link reads "never"** because comp@rev never
  reached 95 % before the blackout either (busy/lora runs at about 30 %). The metric should be
  relative to the pre-blackout level; the series in `--json` has the numbers (busy/lora comes
  back to its 30 % within about 10 s of a 20 s blackout).
- **cons2/telemetry with a 20 s blackout at 10 s**: before 9.4 s to 95 %, after "never (max
  80 %)". Only 5 live contacts and a 10 s window before the clip ends and they go lost; one
  frame per second with two contacts in it. One seed; needs more seeds before reading it as a
  regression of the step-first order.
- **Collapsed transitions** count as misses (grew/shrank flicker, static -> lost inside one
  ladder gap). The harness should fold a transition that was superseded before any copy could
  have been sent into the next one of the same contact rather than count it missed.
- **Latency floor**: 0.1 s is the step, so clean reads 0.1 s for everything; use a finer step
  for a link-delay study.
- **`ce_shown` on hf** went from 5.3 to 8.3 m on cons2 because the floor is 10 s there and
  stopped/unknown contacts now grow at `hi` from the first second. An hf link could afford a
  shorter floor instead.
- **Serial carriers** (0 B per frame): the one-second target frame is then only a loss-size
  question and smaller frames may win under 10 % loss; not measured.
- Chips are not exercised by the harness. Focus is, see section 7.

## 7. Operator focus: one object selected

`tools/sidebyside/scripts/focus.mjs` replays the operator's click. At a set time the operator
clicks the contact holding one footage track. The receiver sends `Focus` up the same lossy
uplink, the edge gives it priority, and the same object is measured with and without the click
against its footage track (the truth). Five loss seeds per cell, measured from the click until
the object leaves the footage.

```
node tools/sidebyside/scripts/focus.mjs                       # all targets, hf lora telemetry contested
node tools/sidebyside/scripts/focus.mjs --target walker --profile lora --seeds 3
node tools/sidebyside/scripts/probe-focus.mjs 21 lora          # the same click on the live page (:8090)
```

Targets: three on the 1080p consensus run (`best2`, 24 tracks, nothing really moves) and two
real movers on the busy 4K parking lot (206 tracks, oversubscribed below 2 kbit/s). Every one of
them sits in a group when clicked, so a click splits the group. *Whole group* stops there.
*Drilled to one* then picks the one individual and releases the group (PROTOCOL.md 4.2).
"Others up to date" is comp@rev over every other live contact. hf and contested are in the
JSON output and follow the same pattern.

| object | link | run | priority after | info age med / p90 | error med / p90 | others up to date | total |
|---|---|---|---:|---:|---:|---:|---:|
| standing person | lora 2k | not selected | - | 0.7 / 1.5 s | 6.6 / 9.0 m | 82 % | 121.6 B/s |
| standing person | lora 2k | whole group | 1.1 s | 1.0 / 2.3 s | 0.7 / 3.1 m | 73 % | 157.5 B/s |
| standing person | lora 2k | drilled to one | 1.1 s | 0.9 / 2.1 s | 0.6 / 3.1 m | 79 % | 141.0 B/s |
| standing person | telemetry 0.6k | not selected | - | 0.8 / 2.0 s | 6.6 / 7.3 m | 61 % | 42.9 B/s |
| standing person | telemetry 0.6k | whole group | 0.6 s | 1.7 / 3.5 s | 0.7 / 2.0 m | 16 % | 43.9 B/s |
| standing person | telemetry 0.6k | drilled to one | 0.6 s | 0.7 / 1.9 s | 0.5 / 1.9 m | 26 % | 44.6 B/s |
| person in group | lora 2k | not selected | - | 2.3 / 5.4 s | 6.1 / 6.1 m | 86 % | 111.5 B/s |
| person in group | lora 2k | whole group | 1.3 s | 1.0 / 2.6 s | 0.5 / 4.6 m | 84 % | 160.8 B/s |
| person in group | lora 2k | drilled to one | 1.3 s | 0.9 / 1.5 s | 0.5 / 4.0 m | 84 % | 133.6 B/s |
| person in group | telemetry 0.6k | not selected | - | 3.7 / 6.6 s | 6.1 / 6.2 m | 71 % | 43.1 B/s |
| person in group | telemetry 0.6k | whole group | 1.2 s | 2.1 / 4.6 s | 0.6 / 4.2 m | 30 % | 46.2 B/s |
| person in group | telemetry 0.6k | drilled to one | 1.2 s | 1.0 / 2.4 s | 0.5 / 4.1 m | 48 % | 45.8 B/s |
| parked car | lora 2k | not selected | - | 1.7 / 5.6 s | 3.4 / 5.5 m | 89 % | 111.5 B/s |
| parked car | lora 2k | whole group | 1.5 s | 1.1 / 1.9 s | 0.4 / 0.7 m | 87 % | 161.1 B/s |
| parked car | lora 2k | drilled to one | 1.5 s | 0.8 / 1.6 s | 0.4 / 0.6 m | 88 % | 134.2 B/s |
| parked car | telemetry 0.6k | not selected | - | 3.2 / 7.9 s | 4.7 / 6.2 m | 76 % | 43.1 B/s |
| parked car | telemetry 0.6k | whole group | 0.4 s | 2.9 / 6.3 s | 0.6 / 1.3 m | 55 % | 46.2 B/s |
| parked car | telemetry 0.6k | drilled to one | 0.4 s | 1.2 / 2.4 s | 0.5 / 0.9 m | 52 % | 45.9 B/s |
| walker (busy) | lora 2k | not selected | - | 4.1 / 11.2 s | 2.9 / 3.9 m | 26 % | 221.5 B/s |
| walker (busy) | lora 2k | whole group | 2.6 s | 2.7 / 5.5 s | 0.6 / 2.1 m | 18 % | 222.4 B/s |
| walker (busy) | lora 2k | drilled to one | 2.6 s | 0.9 / 1.6 s | 0.5 / 1.2 m | 24 % | 221.5 B/s |
| walker (busy) | telemetry 0.6k | not selected | - | 16.9 / 30.3 s | 1.9 / 3.5 m | 10 % | 51.0 B/s |
| walker (busy) | telemetry 0.6k | whole group | 0.5 s | 7.9 / 14.9 s | 2.8 / 8.8 m | 5 % | 52.4 B/s |
| walker (busy) | telemetry 0.6k | drilled to one | 0.5 s | 0.9 / 2.1 s | 0.5 / 0.9 m | 6 % | 51.6 B/s |
| moving car (busy) | lora 2k | not selected | - | 2.4 / 5.6 s | 6.7 / 19.6 m | 37 % | 213.3 B/s |
| moving car (busy) | lora 2k | whole group | 1.3 s | 1.2 / 2.6 s | 0.6 / 10.1 m | 24 % | 215.0 B/s |
| moving car (busy) | lora 2k | drilled to one | 1.3 s | 0.9 / 1.8 s | 0.5 / 8.1 m | 29 % | 214.4 B/s |
| moving car (busy) | telemetry 0.6k | not selected | - | 4.7 / 11.5 s | 6.4 / 18.6 m | 11 % | 51.9 B/s |
| moving car (busy) | telemetry 0.6k | whole group | 0.9 s | 3.3 / 7.0 s | 0.6 / 15.8 m | 4 % | 50.6 B/s |
| moving car (busy) | telemetry 0.6k | drilled to one | 0.9 s | 0.9 / 2.1 s | 0.5 / 4.0 m | 5 % | 50.3 B/s |

What selection buys, drilled to one: the error to the truth falls from the group centroid's 2 to
7 m to about half a metre, and the information is about a second old at the median. That holds
on every link, including the saturated busy scene at 600 bit/s, where the unselected walker's
information is 17 s old at the median. Priority is felt within about a second of the click
(0.4 to 2.8 s; 7 s once under jamming bursts, because the uplink is down too).

What it costs: on hf and lora the rest of the picture loses 0 to 8 points. On 600 bit/s one
focused record at `T_focus` = 1.33 s is half of the link, so the others drop from 61 to 76 % up
to date to 26 to 52 % on the 1080p clip. Focusing a whole group of six at 600 bit/s is the
worst case for the target and the others alike, so the page drills by default: click a group to
split it, click the person.

Fixed on the way (each one had made focus slower than no focus at all):

- **Split children had no priority.** They rode the normal ladder while the group record took
  the 1 Hz focus slot. Children now inherit focus and the group stays on its ladder.
- **Digest acks cancelled focus.** An acked focused contact was moved to `T_floor`, so a parked
  car under focus went silent after 5 s. Acks no longer demote focused records.
- **Focused revisions jumped the queue.** With the halved threshold, a split group of walking
  people revised every step at rank 0 and starved everything else at 600 bit/s (others 1 %
  up to date). Focused records now share at most half the link: `T_focus` stretches with the
  number focused, and a revision waits for the share gap (zero on a fast link).
- **One member could not be focused alone.** A child died with its group's split. A child
  focused on its own is now pinned and follows its track; the pick and the release travel in
  one uplink frame.
- **Lost Focus frames cost 5 s each.** The receiver now retries every second until it hears a
  focused `Contact`.
- **The released group kept showing focused** until its floor repeat. Release now re-sends it at
  once.

Still open:

- **Focus follows a contact, not a person.** In the busy crowd the walker changes contacts
  within half a second of a click at 10 s; the click lands on a group the walker has already
  left. Once drilled, the pinned child follows the track. The click itself cannot.
- **600 bit/s is a real trade.** Half the link for one object is the current share. A lower
  share (a third, say) keeps more of the picture and makes the selected object about 2 s old
  instead of 1. That is an operator-facing choice, not a protocol bug.
- **The standing person's footage track becomes a coasting ghost after 28 s** (a straight line
  at 6.6 m/s with no detections behind it). The measurement stops at 28 s; the tracker should
  not emit coasted rows as observations.

## 8. Military footage

The same pipeline, protocol and operator click on drone footage of military vehicles: two convoy
clips (`amad-test1`, `amad-test2`, watermarked stock previews) and an amphibious column on a beach
(`mvt-test10`, unknown licence). Numbers only; no frames of these clips go in the repo. None of
them shows people on foot.

**Detection.** The MEVA recipe (`best2`, two VisDrone models agreeing at >= 0.75) keeps 2, 2 and 1
tracks: VisDrone never saw armour, and both models sit at 0.4-0.7 on an IFV or a military truck.
The `mil` recipe lowers the vehicle agreement floor to 0.5, tuned on `amad-test1` only and run
once on the two held-out clips (tools/footage/README.md). Eye audit with `tools/footage/tiles.py`:

| clip | split | tracks | real | vehicles in view | note |
|---|---|---:|---:|---:|---|
| amad-test1 forest road, steep | dev | 4 | 4 | about 6 | the trailing launcher truck is never boxed |
| amad-test2 dirt road, oblique | held-out | 3 | 3 | about 8 | the far vehicles are 10-15 px long |
| mvt-test10 beach, low and close | held-out | 4 | 4 on 1 vehicle | 4 | one amphibious vehicle filling the frame breaks into four short tracks as the camera swings |

Precision holds (every track is a real vehicle); recall is a half or less, set by the detector's
training data, not the protocol. A military-trained detector is the next step (section
"Military appearance models" in tools/footage/README.md lists what was tried).

**Operator focus** on a moving IFV and the lead truck of a column (`focus.mjs --target IFV`,
`--target truck`; five seeds; "selected" is the drilled run):

| object | link | run | priority after | info age med / p90 | error med / p90 | others up to date | total |
|---|---|---|---:|---:|---:|---:|---:|
| IFV in convoy | lora 2k | not selected | - | 2.4 / 7.0 s | 9.5 / 11.0 m | 86 % | 66.8 B/s |
| IFV in convoy | lora 2k | selected | 0.7 s | 0.8 / 1.5 s | 0.5 / 1.2 m | 86 % | 115.7 B/s |
| IFV in convoy | telemetry 0.6k | not selected | - | 2.0 / 5.7 s | 9.8 / 12.3 m | 87 % | 40.7 B/s |
| IFV in convoy | telemetry 0.6k | selected | 0.7 s | 1.1 / 2.4 s | 0.5 / 1.3 m | 58 % | 48.2 B/s |
| truck in column | lora 2k | not selected | - | 1.8 / 4.6 s | 10.4 / 11.2 m | 80 % | 57.7 B/s |
| truck in column | lora 2k | selected | 0.7 s | 0.7 / 1.2 s | 0.5 / 9.3 m | 70 % | 111.0 B/s |
| truck in column | telemetry 0.6k | not selected | - | 2.5 / 5.3 s | 10.1 / 10.9 m | 96 % | 33.8 B/s |
| truck in column | telemetry 0.6k | selected | 0.3 s | 0.8 / 1.6 s | 0.6 / 1.5 m | 84 % | 46.9 B/s |

The selected vehicle is reported to half a metre instead of the group centroid 10 m away, with
information under a second old. Two fixes came from this footage:

- **A focused lone contact was absorbed by the next vehicle in the column.** The truck was alone
  when clicked and grouped with the truck behind it before the Focus arrived. Grouping now never
  links the only track of a focused contact to another.
- **A pick that has become a group is split.** If the contact the operator clicked is a group by
  the time it comes back focused, the receiver (page and harness) re-sends it as split, so the
  operator can click the vehicle again. The p90 error of 9.3 m on lora is the time before that.

Caveat: the ground scale comes from box sizes (no camera metadata in these clips) and assumes
civilian vehicle sizes, so metres here are low by an unknown factor (heights fit at 9-20 m where
the drone is clearly higher). Ratios between runs hold; absolute metres do not.

## 9. Busy versus quiet

`tools/sidebyside/scripts/collapse.mjs` replays three real runs at seven link rates (loss 5 %,
delay 0.3 s, two seeds, measured while the footage runs): the busy 4K parking lot (206 tracks,
about 80 objects per frame, 27 live contacts after grouping), a thermal road (91 tracks, 5 live
contacts) and the quiet 1080p lot (24 tracks, mostly parked, 3 live contacts). Position is sent in
1 m steps, so about half a metre is as exact as the receiver can get.

```
node tools/sidebyside/scripts/collapse.mjs [--clips busy,thermal,best2] [--budgets 600,...,64000] [--json out.json]
```

**Busy: where it breaks.** "Current" is the share of live contacts the receiver holds at the
edge's latest revision, "known" the share it holds at all.

| link | busy lot current / known | busy lot error med / p90 | new contact shown after | thermal current / known | quiet lot current / known |
|---|---|---|---|---|---|
| 600 bit/s | 7 % / 38 % | 2.3 / 9.6 m | 19.6 s | 8 % / 44 % | 64 % / 89 % |
| 1.2 kbit/s | 18 % / 60 % | 1.8 / 9.7 m | 9.9 s | 29 % / 76 % | 80 % / 93 % |
| 2 kbit/s | 30 % / 76 % | 1.1 / 7.4 m | 4.7 s | 49 % / 90 % | 86 % / 96 % |
| 4.8 kbit/s | 54 % / 92 % | 0.7 / 4.7 m | 1.3 s | 70 % / 96 % | 87 % / 96 % |
| 9.6 kbit/s | 68 % / 96 % | 0.6 / 3.5 m | 0.5 s | 77 % / 97 % | 88 % / 96 % |
| 19.2 kbit/s | 79 % / 98 % | 0.5 / 2.7 m | 0.3 s | 77 % / 97 % | 86 % / 96 % |
| 64 kbit/s | 81 % / 98 % | 0.5 / 2.6 m | 0.3 s | 76 % / 97 % | 88 % / 96 % |

The quiet lot is saturated at 1.2-2 kbit/s; the busy lot needs about 5-10 kbit/s for the same
picture, roughly in proportion to its live contacts (27 against 3). Below that the circle still
holds the truth 86-92 % of the time and the k-of-n readout shows what is missing.

**Quiet: how far it collapses.** Static and stopped objects, by how long the edge has held them
unchanged (busy lot; the quiet lot is the same within a few points):

| link | unchanged for | error med / p90 | within 1 m | circle / edge ce |
|---|---|---|---|---|
| 2 kbit/s | 0-1 s | 1.6 / 5.6 m | 37 % | 1.35 |
| 2 kbit/s | 3-10 s | 0.6 / 5.0 m | 59 % | 1.36 |
| 2 kbit/s | 10-30 s | 0.5 / 1.0 m | 90 % | 1.90 |
| 9.6 kbit/s | 1-3 s | 0.4 / 1.3 m | 87 % | 1.12 |
| 9.6 kbit/s | 10-30 s | 0.5 / 1.0 m | 90 % | 1.68 |
| 9.6 kbit/s | 30+ s | 0.4 / 1.2 m | 79 % | 1.92 |

The position collapses to the 1 m quantum: once an object has been quiet for 10 s, nine in ten
are within a metre of the edge's estimate, at 2 kbit/s as at 9.6. The circle does not collapse:
it creeps from the edge's `ce` towards `2 x ce` (5.3) between floor repeats. That is the static
rule doing what it says (a missed revision could hide one more `ce` of drift), but on a link that
keeps delivering frames a quiet object's circle could shrink back to `ce` instead; not changed.

**Circles that inflated for nothing, fixed.**

| receiver contacts not fresh (lora, through 60 s after the clip) | before | after |
|---|---|---|
| left the view, busy lot | lost, circle med 146 m, p90 1000 m | out of view, med 4 m, max 5 m |
| left the view, thermal road | lost, med 159 m, p90 1000 m | out of view, med 19 m, max 28 m |
| left the view, quiet lot | lost, med 58 m, p90 1000 m | out of view, med 5 m, max 6 m |
| convoy (camera stopped at the end) | lost, med 738 m | out of view, med 9 m, max 14 m |
| stopped vehicle, thermal, 9.6 kbit/s, quiet 10-30 s | 185 m | 10 m |

- **Out of view** (PROTOCOL.md 3.4 ext bit4): the edge marks a lost contact that left the frame
  (last box within 2 % of the edge) or that it stopped seeing because the camera stopped. The
  harness and page feed the tracker's image boxes to the edge (`scripts/boxes.mjs` reads
  `detlog.npy`); before, the edge had no boxes.
- The freeze at the last sighting and the ack horizon of this round were patches; section 10
  replaces them and the old growth rules with a calibrated model.
- **On the video**, a ring was projected through the homography; for a point off the frame, near
  the camera's horizon, a few metres became hundreds of pixels. The overlay now draws a contact
  outside the frame as an arrow on the border and takes a ring's radius as the median of four
  directions, capped at half the frame.

Lost-in-view contacts kept growing to the 1000 m cap here; section 10 handles them too.

## 10. The error circle as a calibrated belief

The circle used to grow by hand-set rules (a creep for static things, a class-speed term once a
record was "overdue", a 1000 m cap), and the fixes of section 9 patched two of its failures. It is
now one model (PROTOCOL.md 5.3, core `belief.rs`): the circle holds the edge's estimate with
probability 0.95, and it is built from three things the receiver actually knows.

1. **The edge's guarantee.** The edge revises a contact as soon as its estimate leaves the band
   `thr` around the receiver's prediction. That prediction is now computed from the bytes of the
   last record sent, by the same function on both ends (`geo::SentPred`): before, the edge
   predicted from unrounded values and from the send tick, the receiver from rounded ones and the
   observation tick, so "within the band" was not exactly true. A lost contact is no longer revised
   for position (only its prediction moved, the edge had nothing new).
2. **The link's evidence.** Sequence gaps, the measured loss rate, frames saying the queue was
   empty (`cycle_end`), the last frame heard, and the edge's new `Ego.backlog` byte (how long fresh
   news waits for the link). Without `backlog` the saturated busy lot at 600 bit/s held only 76 %
   for contacts the link could not vouch for over 15 s: revisions there are not lost, they queue.
3. **Measured drift.** How far the edge's estimate wanders from a stale prediction, on the footage
   runs (`tools/sidebyside/scripts/drift.mjs`, every revision of every contact followed for 0.5-45 s while
   ignoring later revisions), 95th percentile:

| state / class | 1 s | 5 s | 20 s | fitted p95 | revisions per s |
|---|---|---|---|---|---|
| moving vehicle | 12 m | 40 m | 227 m | 10.8 t^0.93 | 0.85 |
| moving dismount | 5 m | 17 m | 50 m | 5.0 t^0.78 | 0.85 |
| static vehicle | 5.5 m | 9 m | 13 m | 4.7 t^0.40 | 0.25 |
| static dismount | 5 m | 17 m | 25 m | 5.8 t^0.51 | 0.30 |
| stopped / unknown | 4-8 m | 11-30 m | 7-21 m | see belief.rs | 0.2-0.55 |

A tracked contact's circle is its `ce` combined with: the rounding of `pos`, the drift since its
copy was observed capped by the band, and what a missed revision could add (the first missing
revision starts the drift; how likely it is missing comes from the loss rate, the ladder's copies,
the backlog and the last frame heard). A lost contact drifts from its last look. A circle wider than
the camera footprint marks the contact unlocated; the page then shows where it was last seen, its
heading and how long ago.

**Calibration** (`tools/sidebyside/scripts/calibrate.mjs`, two seeds, while the footage runs plus
30 s): "holds edge" is the share of steps with the edge's estimate inside the circle; "seen again
inside" is, for lost contacts the edge later sees again, whether the reappearance was inside the
circle shown just before.

| scene   | link      | samples | holds edge | circle med/p90 m | err med/p90 m | lost circle med m | lost located | seen again inside |
|---------|-----------|---------|------------|------------------|---------------|-------------------|--------------|-------------------|
| busy    | hf        | 49142   | 98.1%      | 5.9/7.8          | 0.5/2.6       | 69.4              | 52.1%        | 87.5% of 16       |
| busy    | lora      | 43524   | 98.3%      | 9.9/18.1         | 0.7/4.8       | 72.0              | 50.9%        | 83.3% of 12       |
| busy    | telemetry | 19700   | 98.6%      | 21.6/62.2        | 1.9/9.8       | 102.0             | 35.6%        | 100.0% of 4       |
| busy    | contested | 39517   | 97.4%      | 12.0/25.8        | 1.2/6.3       | 66.6              | 54.1%        | 87.5% of 8        |
| thermal | hf        | 14463   | 96.6%      | 11.1/25.8        | 0.8/4.7       | 83.8              | 0.0%         | 60.0% of 10       |
| thermal | lora      | 14129   | 96.1%      | 11.9/26.5        | 0.9/6.1       | 85.2              | 0.0%         | 55.6% of 9        |
| thermal | telemetry | 9787    | 96.5%      | 17.1/37.0        | 2.4/14.6      | 88.0              | 0.0%         | 40.0% of 5        |
| thermal | contested | 12798   | 95.9%      | 14.1/30.3        | 1.3/9.1       | 88.1              | 0.0%         | 50.0% of 8        |
| best2   | hf        | 2486    | 98.0%      | 6.6/8.7          | 0.6/1.5       | 33.7              | 48.0%        | -                 |
| best2   | lora      | 2504    | 98.4%      | 6.8/9.4          | 0.6/2.1       | 33.8              | 47.7%        | -                 |
| best2   | telemetry | 2313    | 99.3%      | 8.3/15.4         | 0.9/4.2       | 35.5              | 46.1%        | -                 |
| best2   | contested | 2343    | 99.1%      | 8.4/12.4         | 0.7/2.5       | 37.7              | 44.4%        | -                 |
| convoy1 | hf        | 722     | 100.0%     | 12.3/19.6        | 1.2/2.0       | 153.6             | 0.0%         | -                 |
| convoy1 | lora      | 730     | 99.7%      | 13.6/20.8        | 1.1/2.4       | 155.2             | 0.0%         | -                 |
| convoy1 | telemetry | 746     | 99.6%      | 12.2/25.9        | 1.2/4.2       | 154.6             | 0.0%         | -                 |
| convoy1 | contested | 689     | 99.7%      | 19.0/33.1        | 1.2/4.9       | 155.5             | 0.0%         | -                 |

Every scene and link is at 95.9-100 %. "Holds edge" scores against the edge's estimate at that
step, carried forward at its velocity from its last observation when the contact moves (the edge's
own belief; `ContactView.now_e`): scoring against the last observation instead penalised the
receiver for predicting a vehicle the edge had stopped seeing. By time the link could not vouch for
the copy, every bucket is at 95 % or above except on the thermal road when it could not vouch for
2-15 s (69-90 %, a few hundred of 14 000 samples per link): fast road traffic drifts faster than the
drift pooled over all runs. Against the old rules: the busy lot at 2 kbit/s held 92 % with a 5.4 m
median circle; now 98 % with 9.9 m, the size the evidence supports. The quiet lot holds 98-99 % at
6.6-8.4 m, and parked cars stay at their `ce` for as long as the link vouches for them instead of
creeping to twice it.

Two edge fixes came out of this, and they also lifted the picture: revisions are checked against
the receiver's prediction *at the time of the latest observation*, not at `now` (comparing a frozen
estimate with a moving prediction revised every unseen vehicle several times a second until it was
declared lost), and lost contacts are never revised for position. The busy lot at 2 kbit/s went
from 30 % to 42 % current and 75 % to 86 % known; the convoy from 85 % to 89-91 % current.

Lost contacts: a parked car out of view stays within about 25 m for a minute (a person 50 m), a moving vehicle
reaches the footprint within seconds and is then shown unlocated at its last sighting. Few lost
contacts are seen again in these clips (4-16 per run), and on the thermal road only about half of
them reappeared inside the circle: reappearances there are mostly group centroids that changed
membership while lost, which the drift of a tracked contact does not cover. Too few samples to fit
a separate lost-contact drift; the open item for a longer clip.

## 11. Adaptive detail

The edge picks its detail level (PROTOCOL.md 6.4) from the backlog it measures: tighter groups,
smaller thresholds and every contact when the link keeps up; wider groups, bigger thresholds and
only contacts seen for a while when it does not. Focused contacts are exempt.

```
node tools/sidebyside/scripts/eval.mjs --clip busy,best2 --profile hf,lora,telemetry,dynamic
node tools/sidebyside/scripts/tune-detail.mjs --clips busy,best2 --profiles lora,telemetry --levels 0,1,2,3,4,auto [--seeds 3] [--coarsen-s 1] [--set 4.pos_floor_m=30]
node tools/sidebyside/scripts/probe-revs.mjs --clip busy --profile hf --level 4
```

`tune-detail.mjs` runs each level pinned and the adaptive edge; `probe-revs.mjs` lists which
contacts revise at a pinned level, and why. The `dynamic` profile steps the link through 9.6k,
2k, 600, 2k and 9.6k bit/s over five equal parts of the replay. The edge is told 9600 and is
never told otherwise: it runs paced, taking what the radio drained (`link_credit`) as its token
bucket, as it would behind a modem's flow control.

**Grouping alone does not buy much.** The handoff's first ladder changed only the link distances,
the position factor and a report-age gate. Pinned on the busy lot at 2 kbit/s, level 4 (120 m
groups) cut revisions only from 1132 to 939 and left the picture 35 % up to date, while the
per-track error grew from 5.7 to 35 m. Two causes, from `probe-revs.mjs`:

- a group's centroid jumps whenever a member joins or leaves, and a 120 m group gains and loses
  members all the time (count changes and position revisions of the big groups);
- moving singles stay singles at any distance (cars driving through the lot do not share a course),
  and each revises every 2-3 s on the 30° course, 25 % speed and `ce`-sized position bands.

So each coarse level is also a spatial resolution: a position floor of a third of its link
distance (10, 20, 40 m), at least a share of the group's own radius, a count tolerance (10 %, 20 %,
34 %), and course and speed bands scaled by `dev_factor`. With that, level 4 on the same link
makes 315 revisions and is 82 % up to date. The declared `ce` of a coarse contact is at least its
threshold, so the receiver's circle starts where the edge's tolerance does: honesty at levels 3-4
went from 66-86 % (thresholds raised, `ce` not) to 94-100 %.

The tuning below was done before the calibrated circle of section 10 landed (the edge revising
against the receiver's exact prediction, never for position once lost); the adaptive and
acceptance tables further down are measured after it, on the merged code.

**Pinned levels** (seed 1, before section 10; `trk err` = footage track to the receiver's centroid):

| clip | link | level | up to date | known | k/n | honest | trk err med / p90 | revisions | contacts |
|---|---|---|---|---|---|---|---|---|---|
| busy | 2 kbit/s | 0 | 33.1 % | 75.0 % | 91.5 % | 92.6 % | 3.0 / 9.3 m | 1113 | 20.7 |
| busy | 2 kbit/s | 1 | 30.3 % | 76.1 % | 89.7 % | 91.7 % | 5.7 / 16.0 m | 1132 | 15.5 |
| busy | 2 kbit/s | 2 | 45.7 % | 81.7 % | 95.0 % | 98.0 % | 14.5 / 57.7 m | 886 | 7.3 |
| busy | 2 kbit/s | 3 | 64.4 % | 82.8 % | 91.9 % | 99.8 % | 32.1 / 62.3 m | 515 | 4.9 |
| busy | 2 kbit/s | 4 | 82.2 % | 92.8 % | 96.3 % | 100.0 % | 34.2 / 61.1 m | 315 | 3.7 |
| busy | 600 bit/s | 1 | 7.5 % | 38.4 % | 63.3 % | 84.0 % | 7.8 / 19.5 m | 178 | 15.5 |
| busy | 600 bit/s | 3 | 21.3 % | 49.0 % | 70.5 % | 98.1 % | 35.2 / 63.7 m | 164 | 4.9 |
| busy | 600 bit/s | 4 | 46.3 % | 68.2 % | 83.6 % | 99.8 % | 35.8 / 62.5 m | 162 | 3.7 |
| best2 | 2 kbit/s | 0 | 86.5 % | 96.3 % | 96.4 % | 99.8 % | 4.2 / 7.2 m | 179 | 1.4 |
| best2 | 2 kbit/s | 1 | 85.9 % | 96.6 % | 96.9 % | 99.3 % | 4.7 / 8.2 m | 104 | 1.2 |
| best2 | 2 kbit/s | 2 | 70.0 % | 77.1 % | 86.8 % | 99.5 % | 21.3 / 31.5 m | 43 | 0.7 |
| best2 | 600 bit/s | 0 | 67.2 % | 86.2 % | 92.4 % | 99.8 % | 4.3 / 8.1 m | 109 | 1.4 |
| best2 | 600 bit/s | 1 | 75.3 % | 92.4 % | 95.0 % | 98.0 % | 5.0 / 9.4 m | 96 | 1.2 |
| best2 | 600 bit/s | 2 | 69.3 % | 77.9 % | 76.7 % | 95.2 % | 20.8 / 33.3 m | 59 | 0.7 |

The cost is in the last columns: once the busy lot groups at 30 m and up, the receiver's centroid
is 15-35 m from a typical object. Levels 3 and 4 cost about the same per track (the 60 m groups
already span the lot), so between them level 4 is nearly free. On the quiet lot every coarse level
is worse; level 0 helps on a fat link and hurts on a thin one, so the controller only refines to
0 at 8 kbit/s and up. Level 0's factor is 0.75, not the 0.5 first proposed: at 0.5 the quiet lot
on 9.6 kbit/s fell from 83.3 to 80.9 % up to date; at 0.75 it is 84.0 % with a better per-track
error (4.0 / 7.3 m against 4.8 / 8.2 m at level 1).

**The adaptive edge** (three loss seeds, with section 10; "today" is level 1 pinned):

| clip | link | up to date: today / adaptive | k/n | trk err med / p90 | level (range) | changes |
|---|---|---|---|---|---|---|
| busy | 9.6 kbit/s | 70.3 / 70.1 % | 98.7 / 98.7 % | 4.7 / 15.2 m | 0.5 (0-1) | 1 |
| busy | 2 kbit/s | 46.1 / 63.7 % | 97.2 / 97.7 % | 22.2 / 56.9 m | 2.1 (1-2) | 2 |
| busy | 600 bit/s | 7.2 / 46.7 % | 60.6 / 92.0 % | 32.1 / 65.6 m | 3.7 (1-4) | 3 |
| busy | dynamic | 40.6 / 52.2 % | 97.8 / 96.6 % | 8.3 / 49.3 m | 2.6 (1-4) | 6 |
| best2 | 9.6 kbit/s | 83.7 / 83.7 % | 96.6 / 97.0 % | 4.2 / 7.4 m | 0.2 (0-1) | 1 |
| best2 | 2 kbit/s | 85.8 / 85.8 % | 96.2 / 96.2 % | 4.7 / 8.2 m | 1.0 | 0 |
| best2 | 600 bit/s | 72.4 / 72.4 % | 95.8 / 95.8 % | 5.1 / 9.6 m | 1.0 | 0 |
| best2 | dynamic | 88.0 / 88.5 % | 97.1 / 97.9 % | 4.0 / 7.3 m | 0.2 (0-1) | 1 |

Honesty is 98-99 % in every cell. Section 10 already took a third off the busy lot's revisions,
so at 2 kbit/s the controller now stops at level 2 (before it: level 3 and about 62 %), and the
quiet lot at 600 bit/s no longer coarsens at all. On the dynamic profile (48 s per step) the busy
lot goes to 2 at 42 s (it saturates even 9.6 kbit/s), to 3 and 4 within 25 s of the drop to 2k,
holds 4 through 600 bit/s, and comes back down once the footage ends (95 s) and the scene empties:
6 changes in 238 s, no flaps. Operator focus (`focus.mjs`, five seeds, against the numbers before
both changes): "others up to date" around the walker goes from 26 to 59 % at 2 kbit/s and from 10
to 49 % at 600 bit/s with nobody selected (24 -> 60 % and 6 -> 18 % with one object drilled to); the selected object keeps its update rate and median
error everywhere, its p90 error rises in a few cells (the walker at 600 bit/s 0.9 -> 1.4 m, the
moving car on the contested link 6.6 -> 8.5 m; in the whole-group flow at 600 bit/s 15.8 -> 24.3 m).

Against the proposed acceptance:

| proposal | result |
|---|---|
| busy, 2 kbit/s: up to date 26 % -> >= 70 %, k/n not below today | 63.7 % (today, with section 10: 46.1 %); k/n 97.7 % (97.2 %). Not met at the default `coarsen_s` 2 s; 1 s reached 71 % before section 10 |
| busy, 600 bit/s: 10 % -> >= 60 % | 46.7 %. Not met: see below |
| best2 on hf no worse in up to date or per-track error, level 0 or 1 | 83.7 % vs 83.7 %, 4.2 / 7.4 m vs 4.8 / 8.2 m, levels 0-1 |
| dynamic: <= 6 changes a minute, no flapping | 1.5 a minute, no flap |
| focus.mjs selected object does not regress; core tests pass | update rate and median error unchanged; small p90 rises in a few cells (above); 42 tests pass |

**Why 600 bit/s stops near 45 %.** At level 4 the busy lot still needs about 2.6 revisions a
second (431 in 165 s on an unconstrained link), mostly cars driving through and contacts going
lost; 600 bit/s carries about 1.6 records a second, repeats and `Ego` included. A 40 m floor and
three times the course band are already in; a 60 m floor with six times the bands bought four more
points. The remaining lever is
the 28 B of carrier overhead on every 75 B frame at that rate, not the ladder.

**The threshold is a trade** (before section 10). `coarsen_s` 1 s reaches the 70 % on the busy lot at 2 kbit/s, but
the quiet lot at 600 bit/s then coarsens on a few seconds of confirmations at the start and its
per-track error goes from 5.0 / 9.4 m to 19.3 / 32.2 m (k/n 86 %). At 2 s it stays at 5.5 / 16.2 m.
The default keeps the quiet picture precise; `EdgeConfig::coarsen_s` (`--coarsen-s`) moves it.

**Choices made against the first proposal, from the data:**

- The backlog counts ladder repeats as well as first copies (floor repeats, focused records and
  merge tombstones excluded), as the handoff's "records that are due" says. First copies alone
  said the busy lot at level 3 kept up (0.2-0.8 s) while its repeats starved under 10 % loss; at
  `coarsen_s` 1 s the busy lot at 2 kbit/s was 61.8 % up to date with first copies only (seed 1)
  and 71.2 % with repeats (three seeds).
- Coarsening waits until the backlog stops draining: in the unit-test crowd (40 walkers, 600
  bit/s) the queue the old level left after 2 -> 3 took longer than the hold to drain, and asked
  for 3 -> 4.
- Refining is predicted, not probed. Without the prediction the same crowd went 3 -> 2 -> 3 within
  10 s: at level 3 the one big group hardly revised, so measured demand said anything fits. The
  prediction counts the groups the finer level would make, the births they cost, and the demand
  per contact last seen at that level; a refine undone within 30 s still doubles the wait.
- Start at level 1 on thin links, not 2: the quiet lot at 600 bit/s is best at level 1 (75 %
  against 69 % at 2), and the busy lot coarsens within 5 s anyway.
- A split-focused group's members link only among themselves. Without that, at 30 m the selected
  moving car was folded into a 41-member parked group and its child departed (as an individual
  91 % -> 45 % of the time at 2 kbit/s; 93 % with the rule).

Held-out footage, run once after tuning (FOOTAGE_FINDINGS.md split): convoy2 and amphib are quiet
(one live contact) and the adaptive edge stays at level 1 with identical numbers on every link.
The thermal road (dev, not used for tuning) at 600 bit/s: 9.9 -> 38.4 % up to date, k/n 65.6 ->
87.8 %, per-track error 11.0 / 32.4 -> 13.0 / 43.1 m.

Open:

- **Merge tombstones lag.** They ride at the floor class so a coarsening does not ask for the
  next; until one arrives the receiver holds the absorbed contact beside its new group, and the
  page reads "10 of 7 known" for a while. The receiver could drop a contact whose centroid lies
  inside a newer group from the same edge, or the group record could name what it absorbed.
- **The group centre at levels 3-4** is a poor position (15-35 m from a typical member). The
  receiver draws the radius; the CoT export should carry `max(ce, radius)` as its ce.
- **Lost frames are invisible to the backlog.** With an uplink, digest delivery would be a second
  signal; on simplex the blind spot stays. Without flow control (`paced` off) a link slower than
  the budget looks like loss, not load, and the level does not move.
- **Tuning:** the ladder was tuned on the busy lot and the quiet lot only; the held-out clips are
  too quiet to exercise it.
