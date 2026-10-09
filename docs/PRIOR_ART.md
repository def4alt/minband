# Prior art (researched 2026-10-09)

**Verdict:** the concept (send entity state changes, not pixels; receiver dead-reckons) is not
new. The specific system, perceived 3D entities from a phone/drone camera, deterministic shared
predictor on both ends, state-based repair over a lossy link, live twin with multi-device fusion,
does not exist as an open end-to-end project. That gap is where MinBand sits.

## Direct ancestors

- **DIS dead reckoning (IEEE 1278.1).** Owner runs the same extrapolation model as remote sites;
  sends an Entity State PDU only when real state diverges past a threshold; heartbeat forces a
  periodic update. Our sync protocol is this, plus perception semantics and loss repair.
  Notes: https://www.comp.nus.edu.sg/~cs4344/0607s1/deadreckoning.pdf
  Threshold metric work (spatial threshold alone lets absolute inconsistency grow; timeouts as a
  second trigger): https://mu.eprints-hosting.org/id/eprint/9272/1/TW_bounding%20inconsistency.pdf
  Adaptive thresholds: https://www.academia.edu/795560/An_auto_adaptive_dead_reckoning_algorithm_for_distributed_interactive_simulation
- **DARPA "Semantically-Aware ISR" SBIR (DPA26BZ05-DV019, posted Aug 2026).** Asks for onboard
  processing on Group 1/2 UAS that sends compact semantic packets instead of video over links
  squeezed to single-digit kbps; 90% reduction threshold, 95-99% objective; 2-5 W; dynamic
  bit allocation among ROIs, event summaries, context. Excludes plain video compression and
  cloud. Validates the use case and the budget-controller idea.
  https://www.darpa.mil/research/programs/semantically-aware-isr

## Adjacent academic work

- **Semantic / task-oriented communication for UAVs.** Mostly transmits learned feature vectors
  that a decoder reconstructs, not explicit entity state.
  Context-aware digital semantic comms in UAV networks (ViT features, quantised, RL for
  trajectory/resources): https://arxiv.org/pdf/2601.01430
  Cognitive semantic comms with detector on the server: https://arxiv.org/html/2401.13995v3
  Scene-graph transmission from UAV, virtual environment rebuilt at receiver:
  https://arxiv.org/pdf/2501.04480
  Generative communications overview (UAV sends target + location, base station regenerates the
  scene on a map): https://arxiv.org/pdf/2607.09183
  YOLO-based semantic comms for digital twin construction: https://arxiv.org/pdf/2306.14138
- **Digital twin synchronisation.** A 6G twin paper proposes sending only the difference between
  consecutive semantic feature vectors (10-100x claimed). Semantic-aware twin sync (SA-DTS)
  replaces raw video with compact descriptors. Both are feature-level, not entity-level.
- **Multi-robot scene graphs.** Kimera-Multi (distributed metric-semantic SLAM, communication-
  efficient, resilient to intermittent links): https://arxiv.org/pdf/2106.14386
  Hydra-Multi (joint 3D scene graph from incremental inputs of several robots) and MR-COGraphs
  (communication-efficient object graphs). These share *maps*; we share *live tracks*.
- **Event cameras / ADDER.** Bitrate follows scene change at the pixel level:
  https://arxiv.org/pdf/2508.14996 . Same philosophy one level down the stack.

## Industry

- Edge surveillance cameras send events/metadata instead of continuous video (Genetec "video
  trickling", on-camera tracking patents), but there is no world model on the receiving end.
  https://www.securityworldmarket.com/int/Newsarchive/increased-network-efficiency-and-cost-savings-from-genetec
  https://eureka.patsnap.com/triz-case/edge-hyperzoom-traffic-analytics
- Networked games (Quake 3 delta snapshots, Unreal replication, Overwatch's ECS netcode) use
  per-entity delta against the last acked state plus full snapshots on loss. Our state-repair
  design is closer to these than to DIS.

## What to claim in the presentation

Claim: "bandwidth proportional to surprise". Do not claim the delta idea is new. Claim the
combination (perception-derived entities, shared deterministic predictor on edge and twin,
budget-controlled fidelity, state repair under loss, multi-device fusion, measured against video
and naive metadata) and show the fidelity-vs-bytes curve.
