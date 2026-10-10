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
| `--profile a,b\|all` | lora | the driver's profiles: clean (unlimited), hf 9600, lora 2000 / 10 % / 0.3 s, telemetry 600 / 5 % / 0.05 s, contested (lora + 1-5 s bursts every 3-8 s), blackout |
| `--blackout s:len[,s:len]` | none | scripted blackouts |
| `--budget`, `--loss`, `--delay`, `--uplink-loss`, `--no-uplink` | profile | overrides |
| `--duration` | clip end + 75 s | long enough for the 60 s depart timer and the tombstone ladder |
| `--seed` | 1 | loss and nonce |
| `--focus auto\|<id>` | off | runs the same replay twice, once with one contact focused (track mode, renewed every 5 s), and reports the delta |
| `--dev-factor k` | 1 | experiment knob: the edge's position change threshold becomes `k x max(ce, 2 pos_res)` |
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

