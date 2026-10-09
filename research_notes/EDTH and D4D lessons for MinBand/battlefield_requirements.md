# Battlefield requirements for a low-bandwidth "metadata instead of video" drone ISR system (MinBand), as of 9 Oct 2026

Scope: (a) real link conditions in the Russia-Ukraine war and for NATO/European users, and (b) what MinBand (iPhone/drone edge detection + 3D Kalman tracking + DIS-style dead-reckoned entity deltas over UDP, ~60-800 B/s) must change to go from hackathon demo to fielded use. Defensive and engineering level only.

How this was researched (applies to every section): WebSearch worked. WebFetch was blocked by the egress proxy for almost every primary domain tried (darpa.mil, expresslrs.org, csis.org, twz.com, militarnyi.com, static.rusi.org, defence-industry.eu, insidedefense.com, bwcoconsulting.com, isode.com, meshtastic.org). Most "Cited Findings" therefore come from search-engine summaries of the linked pages, not from reading the full page. Numbers were cross-checked across several results where possible, but exact quotes were not checked against full text. Treat single-source vendor or ministry figures as claims. "Inferences" are this researcher's reasoning, not sourced facts.

---

## Q0. What does the closest official requirement (DARPA Semantically-Aware ISR, DPA26BZ05-DV019) ask of a "metadata instead of video" system?

### Takeaway
DARPA's 2026 SA-ISR SBIR topic is close to an outside spec for MinBand. It asks for a 90% (threshold) to 95-99% (objective) cut in transmitted data compared with full-frame video, at 5 W (interim) and 2 W (final) incremental power. Packets must carry ROIs, tracklets, scene-change descriptors, uncertainty and operator-checkable evidence. It explicitly excludes plain fixed-taxonomy detection and tracking, which is what MinBand does today with COCO YOLO.

### Cited Findings
- Topic number DPA26BZ05-DV019, "Semantically-Aware ISR". DARPA SBIR FY26 Release 5. Its objective is mission-aware semantic communications for low-SWaP tactical ISR platforms that cut transmitted multimodal data while keeping mission context over degraded or intermittent links. — [DARPA program page](https://www.darpa.mil/research/programs/semantically-aware-isr); [DoD SBIR 2026 P1 DARPA instructions](https://www.dodsbirsttr.mil/submissions/api/public/download/solicitationDocuments?solicitation=DOD_SBIR_2026_P1_CBZ&amp=&documentType=INSTRUCTIONS&amp=&component=DARPA&amp=&release=5)
- Phase II threshold is a 90% (10x) reduction against full-frame mission-video baselines. The objective is a 95-99% reduction "with preserved mission utility". — [DARPA program page](https://www.darpa.mil/research/programs/semantically-aware-isr); headline "DARPA seeks AI to slash tactical ISR data by up to 99%" — [Inside Defense](https://insidedefense.com/ai-news/darpa-seeks-ai-slash-tactical-isr-data-99)
- Semantic packets "shall combine high-fidelity regions of interest, tracklets, scene-change descriptors, temporal evidence", plus confidence, uncertainty and explanations the operator can check. — [DARPA program page](https://www.darpa.mil/research/programs/semantically-aware-isr)
- Power: "Threshold = 5 W incremental power during interim testing; final objective = 2 W average incremental power excluding camera and radio." — [DARPA program page](https://www.darpa.mil/research/programs/semantically-aware-isr)
- DARPA describes the topic as an onboard, real-time, low-power layer that understands operator intent and reasons over spatial and temporal context, not ordinary compression or detection. — [DARPA program page](https://www.darpa.mil/research/programs/semantically-aware-isr)
- Exclusions, per a third-party guide: conventional video compression without mission reasoning, fixed-taxonomy object detection or tracking, and large VLMs that cannot run onboard. Weapon release, lethal engagement and named-person identification are also excluded. ITAR-restricted. — [BW&CO guide](https://www.bwcoconsulting.com/fod/darpa-sbir-dv019-semantically-aware-isr); [GrantedAI summary](https://grantedai.com/blog/darpa-fy26-sbir-release-5-hoboken-underwater-3d-printing-semantic-isr-cell-culture-auv-august-26-open-september-23-deadline-strategy-2026)
- Window: open 2026-08-26, close 2026-09-23, so it has already closed as of 2026-10-09. One aggregator mislabels the topic DPA26BZ04-DV019. DARPA's own number is BZ05. — [SBIR portal listing](https://sbir.porbanderwala.cloud/opportunities/f862eed2-25a9-4c34-91ea-7d0f5eeb8e2b); [BW&CO](https://www.bwcoconsulting.com/fod/darpa-sbir-dv019-semantically-aware-isr)

### Inferences
- On bytes alone, MinBand already beats the DARPA objective. Taking 1.5 Mbps (187,500 B/s) H.264 as the baseline, 800 B/s is a 99.57% reduction and 60 B/s is a 99.97% reduction. The gap is mission utility rather than compression. DARPA scores the reduction "with preserved mission utility", so MinBand needs a utility metric: task success, such as "operator correctly reports count, class and location of vehicles in area X within T seconds", measured against video. Bytes per second alone will not do.
- Engineering change: add an "evidence on demand" channel. A JPEG/AVIF chip of a track (for example 64x64 to 128x128 px), sent only when the operator or the budget controller asks for it. Add "scene-change descriptors", such as "new object class appeared in sector", so MinBand matches the DARPA packet concept and gives humans something to check.
- Engineering change: replace fixed COCO classes with a mission-configurable taxonomy, uploaded as a small config. COCO "person/backpack" is both the wrong taxonomy (see Q7) and the category DARPA excludes.
- Engineering change: carry per-entity covariance and an explanation code (why sent: new / class change / DR threshold exceeded / operator request). The current schema of id, class, position, velocity and confidence lacks uncertainty and provenance.

### Gaps
- The full DARPA topic text could not be fetched, so the exact latency, link-rate and test-scenario numbers (if any) are unknown. The 99% figure and the power numbers come from search summaries of the DARPA page and Inside Defense.
- No public award list for DV019 was found as of 2026-10-09.

---

## Q1. Link realities: what bandwidth, latency, loss and jamming do FPV, recon (Mavic, Shark, Vector, Leleka) and long-range drones see, and on which radios?

### Takeaway
The battlefield RF environment runs from fully denied (barrage jamming of 300 MHz-6 GHz near the front, forcing fiber-optic tethers) to multi-Mbps encrypted mesh links (Doodle Labs/Silvus) on fixed-wing ISR at 45-180 km. The common pattern is intermittent connectivity, frequency agility, relays, and constant change in which bands are free. A system that degrades gracefully to sub-kbps and survives long outages is valuable, but most recon drones still have Mbps links when they work. MinBand's niche is the edge of coverage, under jamming, and over relay chains or low-rate bearers.

### Cited Findings
**Losses and EW intensity**
- RUSI (Watling & Reynolds, May 2023) estimated Ukrainian UAV losses of about 10,000 per month, mostly to jamming. An earlier RUSI finding said Russian EW took out about 90% of Ukraine's drones early in the war. This is older data (2023). — [Forbes, Aug 2023](https://www.forbes.com/sites/davidhambling/2023/08/09/how-did-ukraine-beat-russias-drone-jammers/); [EurAsian Times](https://www.eurasiantimes.com/russia-smashing-330-ukrainian-uavs-per-day-uk-report-says-russian-electronic-warfare-wreaks-havoc-on-kyiv/)
- RUSI: in August 2024 Ukrainian air defenders reported Russia flying 1,000-1,500 Orlan-10 and Zala reconnaissance UAV orbits a day over Ukrainian positions. Ukraine had reduced that density, but not eliminated it, by 2025. — [RUSI, Tactical Developments in the Third Year of the Russo-Ukrainian War (Feb 2025)](https://static.rusi.org/tactical-developments-third-year-russo-ukrainian-war-february-2205.pdf)
- "Kill zone": a band of about 10-20 miles on each side of a front of more than 700 miles. A brigade spokesman describes it as "nonstop reconnaissance and surveillance", and thermal cameras make night movement dangerous. — [NPR, 5 Oct 2026](https://www.npr.org/2026/10/05/nx-s1-5925803/drones-buzz-overhead-troops-hunker-underground-in-ukraines-hellish-kill-zone)

**Frequencies and jamming patterns** (secondary analyst coverage; the search summary did not map each claim to one specific URL)
- FPV drones typically use two links, control and video, on common bands such as 900 MHz, 1.3 GHz, 2.4 GHz and 5.8 GHz, and both links can be jammed. Ukrainian developers moved to 900 MHz, 433 MHz and other bands outside the original jammer coverage. Russian EW often targets 2.4 and 5.8 GHz. — [GIS Reports](https://www.gisreportsonline.com/r/ukraine-diy-drones/); [VGI-9](https://vgi.com.ua/en/drones-vs-electronic-warfare-whos-winning-the-battle-for-the-skies-in-ukraine/)
- Russian broadband jammers reportedly cover 300 MHz-6 GHz at once, which makes frequency hopping less effective. A Russian FPV ("Boomerang") reportedly moved its video link above 6 GHz (6-7.2 GHz antenna), per Militarnyi analysis relayed in secondary coverage. Low-reliability secondary sources. — [militarymachine.com](https://militarymachine.com/russia-electronic-warfare-ukraine-drones); [ukraine-war-analytics.com](https://ukraine-war-analytics.com/analysis/electronic-warfare-ukraine-2026.html)
- Doodle Labs says most drones in Ukraine use 2.4 GHz, which draws most Russian EW attention. That is why it pushes multi-band and lower-frequency radios. — [Doodle Labs, low-frequency Mesh Rider](https://doodlelabs.com/news/coming-soon-low-frequency-mesh-rider-radio/)
- ExpressLRS (ELRS) "has become the dominant method of flying kamikaze FPVs" since 2022 because of EW resilience, range and open-source flexibility. Both sides keep moving to new bands and custom hardware. — [Wikipedia: ExpressLRS](https://en.wikipedia.org/wiki/ExpressLRS)
- Moving to a closed protocol (Crossfire) does not help if the whole 900 MHz band is jammed. — [Unmanned Tech Shop comparison](https://www.unmannedtechshop.co.uk/blogs/knowledge-base/fpv-receiver-protocols-elrs-vs-crossfire-vs-ghost)

**Recon/ISR platform links (mostly vendor claims)**
- Ukrspecsystems Mini Shark with the Doodle Labs Helix Mesh Rider: "transmits HD video over 80 km in face of Russian jamming attempts", using six frequency bands and "Sense" auto channel/band switching. — [Doodle Labs + Ukrspecsystems](https://doodlelabs.com/news/ukrspecsystems-partnership/); [Doodle Labs Ukraine](https://doodlelabs.com/news/doodle-labs-empowers-ukrainian-drone-manufacturers-with-helix-mesh-rider-radio-for-enhanced-uas-capabilities/)
- Mesh Rider datasheets: about 80 Mbps at a 20 MHz channel, 40 Mbps at 10 MHz, 20 Mbps at 5 MHz, 12 Mbps at 3 MHz (indicative, at 10 m). URLLC control-channel latency 1.5-10 ms (an older 2.4 GHz page says 3-30 ms). BPSK to 64QAM adaptive modulation. — [Doodle Labs miniOEM datasheet](https://www.mouser.com/datasheet/2/895/Doodle_Labs_miniOEM_2025_1-3197152.pdf); [RM-915 spec](https://www.mouser.com/catalog/specsheets/Doodle%20Labs_RM-915-2J-XE.pdf)
- Shark: encrypted main datalink for real-time Full HD video and telemetry up to 80 km, with a backup channel. Shark-M uses a Silvus radio, claims up to 180 km and AES-256. — [army-technology: Shark](https://www.army-technology.com/projects/shark-unmanned-aerial-system-ukraine/); [Ukrspecsystems Shark-M](https://ukrspecsystems.com/drones/shark-m-uas)
- Leleka-100: radio and video link up to about 45 km. Sources conflict, with one claiming 50-90 km. — [army-technology: Leleka-100](https://www.army-technology.com/projects/leleka-100-unmanned-aerial-vehicle-uav-ukraine/)
- Quantum Systems Vector: AES-encrypted mesh IP datalink, 15+ km with a handheld and 25+ km with sector antennas. The company claims C2 over 35 km. — [Janes, Oct 2022](https://www.janes.com/defence-news/news-detail/netherlands-mod-receives-first-tranche-of-isr-uavs); [Defense Mirror](https://www.defensemirror.com/news/38937/Quantum_Systems_to_Produce_400_Vector_Reconnaissance_Drones_in_Ukraine)
- Silvus StreamCaster LITE 5200 is marketed with an "LPI/LPD and Anti-Jamming" suite ("Spectrum Dominance"). — [Commercial UAV News](https://www.commercialuavnews.com/silvus-technologies-unveils-streamcaster-lite-5200-ultra-low-swap-oem-module-delivering-powerful-manet-radio-performance-for-leading-edge-unmanned-systems)
- DJI Mavic 3: OcuSync 3.0+/O3+. Range is listed as 15 km (Enterprise) or 8 km under CE. In a vendor "Anti-Mavic" jammer trial in Ukraine (drones at about 500 m, operators about 5.3 km away), links were cut beyond 3.4 km. — [Army Recognition](https://www.armyrecognition.com/archives/archives-aerospace-defense/defense-news-aerospace-2025/ukraines-indigenous-recon-drones-challenge-chinas-dji-mavic-3-across-the-battlefield); [Heliguy](https://www.heliguy.com/blogs/posts/dji-transmission-systems-wi-fi-ocusync-lightbridge/); [The Defense Post, Nov 2025](https://thedefensepost.com/2025/11/21/ukraine-anti-mavic-jamming/)

**Fiber-optic, relays, Starlink**
- Fiber-optic FPVs typically operate at 15-25 km, with some teams at 40-50 km (Ukrainska Pravda, Jan 2026). Shmyhal said 352,000 fiber FPVs had been delivered since July 2025. Russian long spools reportedly succeed about 80% of the time to 20 km, against about 30% for some Ukrainian spools at 15 km. — [Ukrainska Pravda, Jan 2026](https://www.pravda.com.ua/eng/articles/2026/01/25/8017810/); [Mezha](https://oboronka.mezha.ua/en/chomu-ukrajina-dosi-vidstaye-u-dronah-na-optovolokni-307959/)
- Fiber was under 5% of the Ukrainian unmanned inventory in May 2025 (older figure; no 2026 share found). — [Atlantic Council](https://www.atlanticcouncil.org/blogs/ukrainealert/fiber-optics-drones-have-emerged-as-critical-kit-for-both-russia-and-ukraine/); [TWZ](https://www.twz.com/news-features/inside-ukraines-fiber-optic-drone-war)
- Fiber drones emit no RF, so they cannot be jammed or found by direction-finding. — [Wikipedia: Fiber optic drone](https://en.wikipedia.org/wiki/Fiber_optic_drone)
- Relays: Brave1 tested repeaters from more than 10 Ukrainian manufacturers that extend UAV coverage "by tens or even hundreds of kilometers". — [Militarnyi](https://militarnyi.com/en/news/brave1-tests-tools-to-increase-drone-communication-range/)
- Relay vendor claims: BlueBird Promin-13 airborne repeater up to 25 km, about 55 min, 1.5 kg payload. Vishchun-5.8 mast repeater up to 25 km. Obriy repeater (4.9-6.2 GHz, video on 1.2-1.3 GHz) for Mavic 3/Autel, 14-15 km. Lithuanian RSI Europe Drone Repeater Kit is "in active combat use" with Ukrainian units. Russia's "Odyssey" airborne relay claims about 70 km. — [BlueBird Promin-13](https://www.blue-bird.tech/en/products/promin-13-drone-repeater-for-fpv-bluebird-tech/); [Flymod Obriy](https://flymod.net/en/item/repeater_obriy); [RSI Europe](https://defence-industry.eu/rsi-europe-launches-drone-repeater-kit-to-extend-fpv-drone-operations-in-ew-contested-terrain-for-ukrainian-and-lithuanian-forces/); [The Defense Post, May 2026](https://thedefensepost.com/2026/05/22/russia-airborne-relay-system/)
- Starlink: Russia mounted Starlink (including Mini) on long-range drones such as Molniya and modified Shaheds, which made them unjammable and longer-ranged. In early February 2026 SpaceX and Ukraine enforced a whitelist and cut off unregistered (Russian) terminals. Whitelist updates are processed once a day. Russian units reportedly fell back to line-of-sight Wi-Fi bridges and relay towers. A Ukrainian general claims Russian drone activity fell by up to 40%. Defense Express says Russian use was not totally stopped. — [CNN, 29 Jan 2026](https://www.cnn.com/2026/01/29/europe/russia-starlink-drones); [CNN, 5 Feb 2026](https://www.cnn.com/2026/02/05/europe/starlink-ukraine-russia-blocked-intl); [Al Jazeera, 10 Feb 2026](https://aljazeera.com/news/2026/2/10/how-does-the-cutoff-of-starlink-terminals-affect-russias-moves-in-ukraine); [Yahoo/UK press](https://www.yahoo.com/news/articles/musk-cutting-starlink-russia-drones-081935078.html)

**Low-rate bearers relevant to MinBand**
- NATO HF (STANAG 5066 over STANAG 4539 modems): measured line utilisation of 83-94% at 75 bps to 9,600 bps on an emulated perfect channel (best case). — [Isode STANAG 5066 measurements](https://www.isode.com/whitepaper/stanag-5066-performance-measurements-over-hf-radio/)
- Isode runs TAK server-to-server over HF using STANAG 5066 Annex X HF-PEP compression (Ed5 draft). TLS is terminated off-air to avoid handshake overhead, and typical CoT XML messages are "a few hundred bytes". — [Isode: Operating TAK over HF Radio](https://www.isode.com/whitepaper/operating-tak-over-hf-radio/)

**Sensor-to-shooter latency context**
- Kropyva and GIS Arta cut the artillery targeting cycle from about 7-10 minutes to under 1 minute in favourable 2022 conditions. In contested (jammed) sectors it stretched to 2-3 minutes. — [Defence Studies, 2026](https://www.tandfonline.com/doi/full/10.1080/14702436.2026.2672089)

### Inferences
- Design target: MinBand should assume three link regimes and switch between them explicitly. (1) Mbps mesh (Doodle/Silvus/Herelink-class): MinBand adds value as a metadata sidecar next to video, feeding C2 maps automatically. (2) Tens to hundreds of bps: ELRS/LoRa telemetry, HF, a deep relay chain, or a jammed link at the coverage edge. MinBand replaces video. (3) Zero for minutes: store and forward, then reconcile on reconnect. The budget controller today covers roughly 60-800 B/s, and should add a tier in the 10-60 B/s range (see Q2) plus an explicit "outage" mode.
- The link fails in bursts, not as random packet loss. The current "repair by resending current state" approach suits bursty loss because no retransmission queue builds up. But dead-reckoned entities will drift during long outages, so the receiver must show staleness clearly: age out by uncertainty growth rather than a fixed timeout, and map this to CoT `stale` (Q5).
- Latency budget: artillery workflows treat 1-3 minutes as normal, so a few seconds of MinBand latency is acceptable for awareness and cueing. Extra RTTs (handshakes, ACKs) are expensive on relays, though. Keep the protocol one-way and idempotent, as it is now.
- Fiber drones and Starlink make some drones "high bandwidth but single-path". MinBand also has a role there: one drone's video saturates its tether or terminal, while the semantic layer can be fanned out to many consumers (Delta, ATAK, artillery) cheaply.

### Gaps
- No independent, measured field data (throughput vs. distance under specific Russian jammers) was found. Platform ranges are vendor claims.
- No measured packet-loss or outage-duration statistics for real front-line links were found. This is the most important missing input for tuning MinBand. Recommendation: get it from Ukrainian partners or Brave1 test events.
- No 2026 share of fiber vs. radio FPVs was found (latest is <5% in May 2025).
- Herelink, Microhard and TBS Crossfire data-rate specifics were not retrieved.

---

## Q2. Could MinBand piggyback on a control/telemetry-only link (ELRS / Crossfire / LoRa / Meshtastic)?

### Takeaway
Yes, but only at the very bottom of MinBand's budget. ELRS telemetry downlink is about 78-625 bps (about 10-78 B/s) at common settings. ELRS 3.5's MAVLink mode forces a 1:2 telemetry ratio, which gives far more, but at the cost of RC update rate. LoRa mesh (Meshtastic) carries compressed CoT at a median of about 87 B per message with a 237 B MTU. MinBand's 60-800 B/s currently fits only the most generous settings.

### Cited Findings
- ELRS telemetry ratio options: Off, 1:128, 1:64, 1:32, 1:16, 1:8, 1:4, 1:2 (plus "Std" and "Race"). At a 500 Hz packet rate: 1:128 gives about 3.9 telemetry packets/s (about 78 bps), 1:64 about 7.8 pkt/s (about 156 bps, about 234 bps with burst), and 1:16 about 31.2 pkt/s (about 625 bps, about 1,172 bps with burst). LINK stats always take part of this. DATA ("Advanced Telemetry") shares bandwidth with MSP. — [ExpressLRS telemetry bandwidth](https://www.expresslrs.org/info/telem-bandwidth/)
- ELRS packet rates go up to 1000 Hz (2.4 GHz FLRC; 900 MHz FSK on LR1121). LoRa modes are slower but longer range: 100 Hz Full / 333 Hz Full LoRa at 2.4 GHz, and 100 Hz Full at 900 MHz. Changing packet rate in flight forces a disconnect. — [ExpressLRS search summary incl. Lua how-to](https://expresslrs.org/quick-start/transmitters/lua-howto); [Wikipedia: ExpressLRS](https://en.wikipedia.org/wiki/ExpressLRS)
- ELRS 3.5 added full bidirectional MAVLink over a single link (one UART at 460,800 baud) with a "stubborn" retrying telemetry sender. MAVLink mode forces a 1:2 telemetry ratio. Telemetry bursts can cut RC update rate to as low as 25% (e.g., 100 Hz to 25 Hz). The release notes claim MAVLink-RC was tested to 126 km without failsafe (test conditions not stated). — [ExpressLRS MAVLink docs](https://www.expresslrs.org/software/mavlink/); [IntoFPV 3.5.0 release thread](https://intofpv.com/t-expresslrs-3-5-0-final-release-now-available)
- Meshtastic TAKPacket SDK: CoT XML of about 400-2,300 B becomes a wire payload of median 87 B, max 184 B, over LoRa with an MTU of 237 B or less. — [Meshtastic TAKPacket-SDK](https://klibs.io/project/meshtastic/TAKPacket-SDK); [Meshtastic TAK protocol docs](https://meshtastic.org/docs/software/apple/developer/tak-protocol/)
- TAK Protocol v1 "Mesh SA" UDP framing: 3-byte header 0xBF 0x01 0xBF followed by a protobuf TakMessage. Stream (TCP) framing uses a varint length. — [takproto docs](https://takproto.readthedocs.io/en/latest/tak_protocols/)
- HF via STANAG 5066: 75-9,600 bps (see Q1). — [Isode](https://www.isode.com/whitepaper/stanag-5066-performance-measurements-over-hf-radio/)

### Inferences
- Arithmetic: 60 B/s = 480 bps, which needs ELRS at about 1:16 or richer at 500 Hz. 800 B/s = 6.4 kbps, which no common ELRS telemetry setting supports. To piggyback on FPV-class control links, MinBand needs a 10-50 B/s tier. That means about 1-3 entity updates per second at about 8-16 B each.
- Engineering changes to reach that tier:
  1. A bit-packed fixed-point wire format: 16-bit position in decimetres within a local tile, 8-bit quantised velocity, 4-bit class, 4-bit confidence, and 8-12-bit entity ID. Use varint/delta encoding against the last acknowledged-by-assumption state.
  2. Priority scheduling by "information value" (new entity > class change > DR error above threshold > refresh), rather than round-robin.
  3. A hard cap on entity count, so that more than N entities collapse into "group/cluster" reports (centroid, count, class histogram).
  4. Message sizing to the bearer MTU: ELRS telemetry frames are small, LoRa/Meshtastic up to about 237 B. Never rely on IP fragmentation.
  5. A MAVLink transport option. Wrap MinBand deltas in a custom MAVLink 2 message, or in `TUNNEL`/`DATA` style payloads, so they ride existing ELRS-MAVLink, Herelink and SiK telemetry paths without a new radio. MAVLink is the realistic piggyback path for PX4/ArduPilot drones.
- Taking telemetry bandwidth from the control link trades directly against RC update rate (ELRS 1:2 forced in MAVLink mode). For FPV strike drones this is likely unacceptable in the terminal phase. MinBand on control links fits loitering recon and relay drones better than strike FPVs.
- Meshtastic's 87 B median for a compressed CoT is a useful benchmark. If MinBand can deliver about 10-20 B per entity delta, it is 4-8x denser than compressed CoT on the same LoRa bearer. That is a concrete, defensible claim to test.

### Gaps
- Per-packet payload size of ELRS telemetry frames and the effective MAVLink throughput at 50/100/250 Hz could not be confirmed. The expresslrs.org pages could not be fetched.
- TBS Crossfire MAVLink throughput was not found.
- Whether Ukrainian units' modified ELRS forks (frequency-hopping across custom bands) keep telemetry is unknown.

---

## Q3. Emission control / LPI-LPD: how do transmissions expose drones or operators, and does "fewer, smaller bursts" help?

### Takeaway
RF emissions do get operators found and struck. Russia's Rubicon unit specialises in radio reconnaissance to locate Ukrainian drone operators, and both sides field emitter-homing and SIGINT tools. Fewer and shorter transmissions reduce intercept probability and dwell time for direction-finding. But the ground station uplink, beacons, periodic heartbeats and protocol fingerprints matter as much as payload bytes. MinBand should present this as "reduced time-on-air and a configurable emission policy", not as "undetectable".

### Cited Findings
- Russia's Rubicon drone unit "specializes in electronic-warfare and radio-signal reconnaissance, which helps to effectively locate Ukrainian drones and their operators". One brigade reportedly lost up to 70% of its drone operators in a week to Rubicon targeting (single-source interview). — [RFE/RL](https://www.rferl.org/a/russia-drone-rubicon-secret-ukraine-war/33532804.html)
- Shipovnik-Aero is described as an automated RF direction-finding and jamming system that can identify, locate and jam multiple drone control channels. RUSI highlighted it as particularly effective because of its low signature and its ability to imitate other emitters. Secondary sources. — [ukraine-war-analytics.com](https://ukraine-war-analytics.com/weapons/electronic-warfare-systems-march-2026.html); [EurAsian Times on RUSI](https://www.eurasiantimes.com/russia-smashing-330-ukrainian-uavs-per-day-uk-report-says-russian-electronic-warfare-wreaks-havoc-on-kyiv/)
- A Ukrainian balloon-mounted passive SIGINT sensor ("Aero Azimuth") locates Russian drone operators from control, telemetry and data-link emissions at about 9 miles, and passes locations to strike assets (Sept 2024, older). — [TWZ](https://www.twz.com/air/balloon-based-sensor-that-pinpoints-location-of-drone-operators-emerges-in-ukraine)
- Ukraine presented a drone that automatically detects and attacks Russian EW emitters (6 Oct 2026). A Brave1 Market listing (SHOT-L1) describes homing on the strongest emitter in an area. — [Ukrainska Pravda, 6 Oct 2026](https://www.pravda.com.ua/eng/news/2026/10/06/8056725/)
- Monitoring both ends of a link can geolocate the control station. Precision depends on sensor geometry and encryption status. — [drone-warfare.com RF detection](https://drone-warfare.com/counter-uas/rf-detection/)
- DJI drones broadcast unencrypted DroneID/AeroScope data, including the pilot's location. Ukraine believed Russia used AeroScope to find and strike operators (2022, older). Casualty numbers are disputed, and DJI later discontinued AeroScope (2023). — [C4ISRNET, Oct 2022](https://c4isrnet.com/battlefield-tech/2022/10/17/how-ukraine-learned-to-cloak-its-drones-from-russian-surveillance); [RFE/RL](https://www.rferl.org/a/drone-detection-war-ukraine-china-russia/31943191.html); [Mezha, Mar 2023](https://mezha.ua/en/2023/03/06/dji-has-quietly-discontinued-the-aeroscope-drone-detection-system-that-could-have-been-used-by-the-russians/)
- Fiber-optic drones do not reveal operator or drone location to direction-finding and can idle on the ground. — [Wikipedia: Fiber optic drone](https://en.wikipedia.org/wiki/Fiber_optic_drone)
- Basic intercept model: intercept probability = detection probability x fraction of time the intercept receiver is listening (RAND, 2004, older). — [RAND TR-159](https://www.rand.org/content/dam/rand/pubs/technical_reports/2004/RAND_TR159.pdf)
- LPI techniques for UAS datalinks include directional transmission and low-duty-cycle methods (LDCM). The text warns not to assume a moving UAV is inherently hard to intercept. — [K-State UAS textbook, Ch. 13](https://kstatelibraries.pressbooks.pub/unmannedaircraftsystems/chapter/chapter-13-data-links-functions-attributes-and-latency/)
- A BAE LPD waveform patent uses bursts with random transmission timing and power limits to reach a 3:1 or better communication-to-intercept range ratio. — [BAE patent US 8,976,837](https://www.freepatentsonline.com/8976837.html)
- The US Navy sought a lightweight LPI/LPD link for small UAVs (99%/3-sigma at 30 km). This is a 2005 requirement, not a result. — [Navy SBIR 05.1 N05-198](https://www.navysbir.com/05_1/198.htm)

### Inferences
- What fewer bytes buys: (a) less time-on-air, so lower intercept probability under the RAND model and less dwell for DF triangulation; (b) the option to use narrower channels, lower power or higher processing gain (spreading or FEC) for the same information, which improves both LPD and anti-jam margin; (c) the option to send nothing until there is something worth sending. It does not change the radio's own beacons, sync and hop patterns, or the GCS uplink. The operator's location is usually exposed by the ground transmitter.
- Engineering changes:
  1. Make the budget controller an emission-policy controller with modes such as SILENT (record on board, no TX), BURST-ON-EVENT (TX only on a new or high-value track, or on request), PERIODIC-MIN, and NORMAL. Expose a "max TX duty cycle" knob as well as B/s.
  2. Remove fixed-period heartbeats, which DIS-style protocols have by default. Use jittered or randomised timing so MinBand traffic has no periodic fingerprint.
  3. Keep the protocol strictly one-way by default (no ACKs or NACKs from the ground). MinBand's "resend current state" repair already allows this, and it keeps the operator side silent. Make uplink requests (chips on demand, re-tasking) rare and batched.
  4. Pack several entity updates into one burst rather than streaming. Fewer, larger bursts beat many tiny ones because each packet carries preamble and sync overhead and is a separate detection opportunity.
  5. Never broadcast own-position or operator-position in clear (the AeroScope lesson). Encrypt everything, including platform pose.
- Honest framing for a pitch: "MinBand reduces RF time-on-air by about 2-3 orders of magnitude vs. video and lets the unit choose when to emit. It does not make a radio LPD; that is the waveform's job (Silvus/Doodle/hopping/fiber)." Overclaiming LPD would be spotted at once by military evaluators.

### Gaps
- No public quantitative model or field data links burst duty cycle to DF localisation error or time-to-locate for drone links specifically.
- No confirmed public case study names the DF method used in a specific operator strike. The Rubicon attribution comes from a single source.
- Russian system names (Torn, Moskit) as operator-locators could not be confirmed.

---

## Q4. Geolocation: how do ISR systems convert detections to MGRS/WGS84, especially under GNSS denial, and what accuracy is needed?

### Takeaway
Fielded systems geolocate by combining platform pose (GNSS/INS or visual navigation) with gimbal angles and range. Range comes from a laser rangefinder (best, about 8 m at 10 km in tests) or from ray-casting against a DEM, whose errors grow to tens or hundreds of metres at oblique angles. Under GNSS denial, visual map-matching gets about 20-30 m. MinBand's marker-relative local frame cannot feed any C2 system today. Adding a geodetic anchor and propagating uncertainty is the largest single gap.

### Cited Findings
- DEM vs. line-of-sight without a rangefinder: at 80 degrees off-nadir with 100 m target elevation, a DEM method cut geolocation error from about 600 m to about 180 m. A 50 m target-elevation error alone produced about 130-150 m ground error at 0-35 degrees off-nadir. — [Sensors 2022, long-range oblique reconnaissance geolocation](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8914804/); [Drones 2024, multi-view](https://doi.org/10.3390/drones8050177)
- Laser rangefinder: flight test reported geolocation accuracy under 8 m at 10 km target distance. A manned test at 8,000 m altitude reported RMS under 8 m. LRF range limits its use at long oblique ranges. — [ResearchGate: LRF-based geolocation](https://www.researchgate.net/publication/332296100_Target_geo-location_based_on_laser_range_finder_for_airborne_electro-optical_imaging_systems); [Sensors 2022](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8914804/)
- Kalman filtering a moving target from a gimbal-camera UAV (simulation): EKF mean errors of 0.82 m north and 0.59 m east, against raw errors of about 1.5 m. — [Aerospace 2025 (MDPI)](https://www.mdpi.com/2226-4310/12/12/1065)
- Accuracy depends on drone elevation, camera attitude/quaternion, lens distortion and gimbal stability. — [arXiv 2509.20906](https://arxiv.org/pdf/2509.20906)
- GNSS-denied navigation: Ukrspecsystems Shark-M (July 2026) launches without GNSS and then matches live video against satellite imagery. — [The Defense Post, 28 Jul 2026](https://thedefensepost.com/2026/07/28/ukraine-shark-m-drone/)
- Ukrainian OSCAR optical relocalisation system (about Jan 2026). — [NextGen Defense](https://nextgendefense.com/ukraine-drones-vision-navigation/)
- NaviLoc (Kyiv-affiliated, Jan 2026): 19.5 m mean localisation error on 50-150 m altitude rural flights, using visual place recognition plus VIO. — [Drones 10(2):97](https://www.mdpi.com/2504-446X/10/2/97)
- UAV Navigation (Spain) VNS01 adds terrain-referenced navigation plus satellite map matching. It claims about 30 m error bound "under favorable conditions" (Sept 2026, vendor claim). — [UAS Weekly, 2 Sep 2026](https://uasweekly.com/2026/09/02/uav-navigation-enhances-vns01-for-precision-drone-navigation-in-gnss-denied-environments/)
- Visual methods degrade in adverse weather, low light and low visibility. — [GPS World / review summary](https://www.gpsworld.com/uav-navigation-enhances-vns01-for-precision-drone-navigation-in-gnss-denied-environments/)
- Auterion's software on SkyFall "Shrike" drones (July 2026) reportedly navigates using only the primary camera. — [Aeronaut.media](https://aeronaut.media/news-en/uavs-drones-news-en/en-ukraines-drones-get-ai-upgrades/)
- Kropyva workflow: a user marks the enemy position on a tablet, and the coordinates go to the nearest battery. It works offline. — [GUR](https://gur.gov.ua/en/content/kropyva-diie-vluchno); [Defence Horizon Journal](https://tdhj.org/blog/post/mosaic-warfare-ukraine/)
- CoT carries position as lat, lon, hae plus `ce` (circular error) and `le` (linear error). — [FreeTAKServer CoT docs](https://www.mintlify.com/FreeTAKTeam/FreeTakServer/concepts/cot-messages)
- MISB ST 0601 has dedicated tags for Target Location Lat/Lon/Elevation (40/41/42) and Frame Center Lat/Lon/Elev (23/24/25). — [impleotv MISB 0601 tags](https://www.impleotv.com/content/klvinspector/help/supported-misb-tags); [Gremsy KLV keys](https://docs.gremsy.com/payloads/vio/gremsy-payloadsdk/supported-klv-keys-misb-0601-full)

### Inferences
- Accuracy tiers (reasoned from the above, not an official standard): awareness and cueing work with tens of metres (about 20-50 m: "vehicle in this tree line"). Fires handoff wants under 10 m CE (LRF-class). MinBand should label every output with its accuracy class and never present awareness-grade positions as target-grade.
- Arithmetic: a 10-17 mrad heading error (typical magnetic compass) gives about 50-85 m lateral error at 5 km. Heading and attitude, more than detection, set geolocation error at range.
- Engineering changes:
  1. Replace the printed-marker origin with a geodetic anchor: the platform's nav solution (GNSS/INS when available, visual navigation when not) gives the WGS84 pose of the camera. Transform each track to ECEF then WGS84, and output MGRS for human display. Keep the local ENU tile only as an internal wire-compression frame (it is excellent for byte efficiency) and send the tile origin once, signed.
  2. Range source abstraction: iPhone LiDAR becomes, on a drone, a laser rangefinder (single-point on the gimbal boresight), a DEM ray-cast (SRTM/Copernicus DEM preloaded), or a flat-earth fallback. Tag each fix with its method in the output (CoT `how`; MinBand provenance field).
  3. Full error propagation: combine platform pose covariance, gimbal angle noise, range error and DEM error into a per-track covariance, then into CoT `ce`/`le` and MinBand confidence. The Kalman tracker already has a covariance, so expose it.
  4. Multi-view triangulation: a loitering drone sees the same stationary target from several bearings. Fusing those bearings (the "repeated imaging" method above) cuts DEM-induced error substantially and suits MinBand's tracker.
  5. GNSS spoofing awareness: when the nav solution is suspect, mark outputs as degraded rather than silently shifting the whole world.

### Gaps
- No official NATO or Ukrainian accuracy requirement (e.g., CE90 for observed-fire target handoff) was found in public sources.
- The iPhone LiDAR range limit (believed to be about 5 m) was not checked in this pass. It is clearly irrelevant at drone altitudes, but the exact figure is unverified here.
- No public data was found on Delta's or Kropyva's internal coordinate/accuracy handling.

---

## Q5. Interoperability: which output formats do Ukrainian and NATO users need, and which are quick wins?

### Takeaway
The quick win is Cursor on Target (CoT) for ATAK/WinTAK/iTAK. It is simple XML or protobuf, the de facto coalition tactical lingua franca with more than 500,000 TAK users, and some Ukrainian radios already bridge to it. Delta (Ukraine's main SA system, mandated at all levels since Aug 2025) has no public third-party API, so integration means going through Ukraine's MoD or Brave1. MISB ST 0601/0903 KLV (STANAG 4609) is the NATO ISR metadata standard but assumes a video stream. STANAG 4586, Link 16/VMF and JREAP are long efforts and not a fit for a startup.

### Cited Findings
**CoT / TAK**
- CoT message = `event` (attributes uid, type, time, start, stale, how) + `point` (lat, lon, hae, ce, le) + `detail`. Type is hierarchical: `a-{affiliation}-{dimension}-{function}`, e.g. `a-h-G` = hostile ground. `how` codes how the position was produced (e.g. `m-g` machine/GPS, `h-g-i-g-o` human). Sources disagree on the default stale (60 s in one, the example uses 1 day). — [FreeTAKServer CoT docs](https://www.mintlify.com/FreeTAKTeam/FreeTakServer/concepts/cot-messages); [cotlib (Go)](https://identichosting.duckdns.org/NERVsystems/cotlib)
- TAK Protocol v1 uses protobuf (proto3) `TakMessage`. UDP mesh uses the header 0xBF 0x01 0xBF. — [takproto docs](https://takproto.readthedocs.io/en/latest/tak_protocols/)
- TAK has more than 500,000 users globally (DoD, DHS, coalition partners, MoDs). Variants: ATAK-MIL (controlled distribution), iTAK, WinTAK, TAKX, TAK Server, WebTAK. — [Breaking Defense, Nov 2025](https://breakingdefense.com/2025/11/evolution-and-future-of-the-tactical-assault-kit-for-soldiers-and-special-operators/); [TAK.gov](https://tak.gov/solutions/military)
- A vendor view: CoT is not part of formal NATO accreditation but has become the "de-facto tactical lingua franca" in coalition operations. Bridging to NATO C2 means mapping CoT to MIP4-IES or ADatP-34 NFFI. — [Corvus Intelligence blog](https://corvusintell.com/blog/interoperability/cot-tak-nato-interoperability/)
- NATO Federated Mission Networking (FMN) has 43 members (32 NATO nations, 11 non-NATO, plus the NATO Command Structure). — [Wikipedia: FMN](https://en.wikipedia.org/wiki/Federated_Mission_Networking)
- C-UAS vendor MyDefence sends drone threat tracks as standard CoT to any TAK unit "without the need for special plugins". This precedent maps directly onto MinBand. — [MyDefence ATAK integration](https://mydefence.com/technology/atak-integration/)
- Ukrainian HIMERA radios claim integration with Kropyva, ComBat Vision and ATAK (ATAK tested via CivTAK). — [Militarnyi](https://militarnyi.com/en/news/ukrainian-himera-radios-integrated-into-atak-situational-awareness-system/)

**Ukrainian systems**
- Delta: developed with Aerorozvidka. Ingests reconnaissance data, live video, target vetting and workflow. Defence Minister order of 6 Aug 2025 rolled it out across all levels of the Defence Forces. — [Wikipedia: Delta](https://en.wikipedia.org/wiki/Delta_(situational_awareness_system)); [The Defender, Aug 2025](https://thedefender.media/en/2025/08/delta-full-implementation/)
- The Vezha streaming module (Oct 2024) brings live UAV feeds into Delta and lets operators place markers on the Delta map. The Vezha mobile app is distributed only through military MDM. The precise integration between Delta and drones is withheld for OPSEC. No public developer API or partner program was found. — [Ukraine MoD](https://mod.gov.ua/en/news/kateryna-chernohorenko-the-battlefield-video-analysis-platform-known-as-vezha-is-now-accessible-within-the-delta-combat-system); [Wikipedia: Delta](https://en.wikipedia.org/wiki/Delta_(situational_awareness_system))
- CSIS has assessed whether Delta already amounts to functional CJADC2 (content not retrieved). — [CSIS](https://www.csis.org/analysis/does-ukraine-already-have-functional-cjadc2-technology)
- Kropyva: artillery fire-control/BMS. A CSIS-cited figure says 90-95% of Ukrainian artillery units use it (secondary). It works offline under degraded connectivity. — [UNITED24](https://united24media.com/war-in-ukraine/ukraines-secret-weapon-kropyva-software-4026); [Defence Horizon Journal](https://tdhj.org/blog/post/mosaic-warfare-ukraine/)

**NATO ISR metadata**
- STANAG 4609 video carries SMPTE ST 336 KLV metadata. MISB ST 0601 is the UAS Datalink Local Set (platform, sensor, frame center, target location). MISB ST 0903 is VMTI (moving-target detections and tracks), embedded as ST 0601 tag 74. A validator rule requires tags 2 (Precision Time Stamp), 23 and 24 (frame center) in the parent packet. — [impleotv STANAG 4609](https://impleotv.com/2025/03/11/stanag-4609-isr-video/); [impleotv VMTI parent test](https://impleotv.com/content/stinspector/help/docs/tests/KLV/misb0903-embedded-vmti-parent-metadata/)
- Open tooling exists: `pymisb` (ST 0903 decode-only) and the `stanag4609` Python toolkit (ST 0601 and 0903 VMTI). — [pymisb on PyPI](https://pypi.org/project/pymisb/); [stanag4609 docs](https://stanag4609.readthedocs.io/)

**European context**
- EU Drone Defence Initiative and Eastern Flank Watch: launched Q1 2026, initial capacity by end 2026, EDDI fully functional end 2027, EFW functional by end 2028. Both are framed as "interoperable" and aligned with NATO C2. No specific data standard is named. Finland and Poland co-lead EFW. — [EPRS briefing](https://www.europarl.europa.eu/RegData/etudes/ATAG/2025/777962/EPRS_ATA(2025)777962_EN.pdf); [European Commission action plan, Feb 2026](https://ec.europa.eu/commission/presscorner/api/files/document/print/en/ip_26_364/IP_26_364_EN.pdf); [ESD, Mar 2026](https://euro-sd.com/2026/03/articles/exclusive/49854/europes-drone-wall-ready-eddi-go/)

### Inferences
- Quick wins, in about days:
  1. A laptop-side CoT exporter: each MinBand entity becomes a CoT event with uid `minband.<platform>.<trackid>`, type from a class to 2525/CoT mapping (e.g. person to `a-u-G-U-C-I`, vehicle to `a-u-G-E-V`; default affiliation "unknown" `u`, never auto-"hostile"), `how="m-..."` machine-generated, `ce`/`le` from covariance, and `stale` = time until DR uncertainty exceeds threshold. Send it as TAK v1 protobuf over UDP mesh or to a TAK Server. Test with WinTAK/iTAK/ATAK-CIV and FreeTAKServer.
  2. A GeoJSON/KML export for anything else.
- Medium effort (weeks): a MISB ST 0601 + 0903 VMTI KLV emitter. When video does exist (Mbps regime), MinBand tracks become standards-compliant VMTI metadata alongside STANAG 4609 video, and NATO ISR exploitation tools can ingest them. It can also be emitted without video as a "metadata-only" KLV stream, but check whether consumers accept that (tags 2/23/24 are needed, so ST 0601 platform/frame-center data must also be populated).
- Long efforts (months to years, need a government sponsor): Delta integration (needs MoD/Brave1 access, MDM, Delta auth), Kropyva (fires domain, safety-critical), STANAG 4586 (full UAV control interoperability), Link 16/VMF/JREAP (crypto, certification, platform integration). For a startup these are partnership tasks, not engineering tasks.
- The most credible route to Ukrainian users is through Brave1 (test events, Dataroom, Brave1 Market listing) and through integration with systems that already reach Delta, such as Vezha-class video aggregators. MinBand could sit as "metadata sidecar to Vezha".
- Symbology: CoT type strings map to MIL-STD-2525/APP-6 SIDCs, so doing CoT correctly covers most symbology needs for TAK.

### Gaps
- Delta's ingest interfaces and data model are not public.
- No primary source was found for any European army's formal ATAK/CoT adoption (e.g., Bundeswehr), or for a NATO decision on CoT in FMN.
- The exact MISB ST 0601 encodings for tags 40-42 and the current ST 0903 revision (0903.6 per one toolkit) were not verified against MISB text.

---

## Q6. Security: what encryption and authentication do such links need, and what is the minimal-overhead version for a tiny link?

### Takeaway
Every fielded recon datalink in this space advertises AES (usually AES-256; Shark, Shark-M, Vector, Doodle Labs FIPS 140-2/3 options). Captured drones are exploited, and unencrypted broadcasts have exposed pilots (AeroScope). MinBand needs authenticated encryption with replay protection, per-mission keys and safe key handling on capture. On a 60 B/s link, standard AES-GCM framing (28 B per packet) is a heavy overhead, so a compact AEAD framing is needed.

### Cited Findings
- Shark's AES-256 module is described as enabling operation up to 80 km under EW (secondary). Shark-M: AES-256. Leleka-100: encrypted digital control channel. Vector: AES-encrypted mesh IP. — [Wikipedia: Ukrspecsystems Shark](https://en.wikipedia.org/wiki/Ukrspecsystems_Shark); [Ukrspecsystems Shark-M](https://ukrspecsystems.com/drones/shark-m-uas); [AvPay Leleka-100](https://avpay.aero/company/ukrspecsystems/product/leleka-100-drone-for-sale/); [Janes](https://www.janes.com/defence-news/news-detail/netherlands-mod-receives-first-tranche-of-isr-uavs)
- Doodle Labs Mesh Rider: 128-bit AES at full throughput, 256-bit AES capped at 12 Mbps, FIPS 140-2 Level 2 or optional FIPS 140-3. — [Doodle Labs datasheet](https://www.mouser.com/datasheet/2/895/Doodle_Labs_miniOEM_2025_1-3197152.pdf); [Doodle Labs product page](https://doodlelabs.com/?p=2921)
- NIST SP 800-232 (Aug 2025) standardises Ascon-AEAD128: 128-bit key, 128-bit nonce, tag truncatable to 32-128 bits. The IETF COSE draft says tags shorter than 64 bits only after careful risk analysis. A fresh nonce is needed per encryption under the same key. — [IETF draft-ochkas-cose-ascon](https://datatracker.ietf.org/doc/draft-ochkas-cose-ascon/)
- AEAD alone does not stop replay of an earlier valid message, so a sequence number, timestamp or equivalent is needed. — [IACR ePrint 2026/1882](https://eprint.iacr.org/2026/1882.pdf)
- MAVLink 2 signing: SHA-256-based signature on every message when a key is set. SETUP_SIGNING carries a 32-byte key and a 64-bit initial timestamp. Newer PX4 accepts SETUP_SIGNING only over USB. Replay state is tracked per (system, component, link ID), with monotonic timestamps persisted across restarts. — [PX4 message signing](https://docs.px4.io/main/en/mavlink/message_signing.html); [XMAVLink signing docs](https://hexdocs.pm/xmavlink/XMAVLink.Signing.md)
- Capture and exploitation: Russia's MoD claimed it decrypted routing data and the flight controller from a Ukrainian drone downed over Novgorod in Dec 2025 (unverified state claim via a mirror of Russian media). Ukraine reportedly booby-traps captured FPVs with malware that damages USB or blocks reflashing (Russian claims relayed in Forbes/Euromaidan). HUR disrupted Russian drone "friend-or-foe" servers. — [Russian state media mirror (low reliability)](https://92.5.118.90.traefik.me/20260101/decrypted-data-from-uav-shot-down-in-novgorod-region-handed-over-to-us-side-1123400837.html); [Euromaidan Press / Forbes](https://euromaidanpress.com/?p=331999); [Yahoo News](https://news.yahoo.com/ukrainian-cyber-specialists-disrupt-russias-164500594.html)
- DJI DroneID/AeroScope signals were unencrypted and revealed pilot location (2022). — [C4ISRNET](https://c4isrnet.com/battlefield-tech/2022/10/17/how-ukraine-learned-to-cloak-its-drones-from-russian-surveillance)
- Delta and Vezha rely on system authentication, with the app distributed via military MDM. — [Ukraine MoD](https://mod.gov.ua/en/news/kateryna-chernohorenko-the-battlefield-video-analysis-platform-known-as-vezha-is-now-accessible-within-the-delta-combat-system)
- Isode terminates TLS off-air when running TAK over HF to avoid handshake overhead on narrow links. — [Isode](https://www.isode.com/whitepaper/operating-tak-over-hf-radio/)

### Inferences
- Overhead arithmetic: AES-GCM with an explicit 12-B nonce and 16-B tag adds 28 B per packet. At 60 B/s with one packet per second, that is about 47% overhead. A compact framing gets this to about 10-12 B: an implicit nonce built from (key epoch, sender ID, 32-bit packet counter), sending only the 4-B counter, plus a 64-bit truncated tag (AES-GCM-64 or Ascon-AEAD128 with t=64). That is about 17-20% at 60 B/s. Batching more entities per packet reduces it further.
- Minimal MinBand security profile (engineering changes):
  1. AEAD on every packet (AES-256-GCM or Ascon-AEAD128), with the 4-B monotonic counter doubling as an anti-replay window at the receiver.
  2. Pre-shared per-mission keys loaded by a wired or USB step before flight, matching the PX4 USB-only SETUP_SIGNING model. No in-band key exchange or handshake (handshakes cost RTTs and emissions).
  3. Key rotation per sortie or epoch. Keys held in RAM only, with zeroise-on-tamper or zeroise-on-crash where hardware allows. No long-term keys on the airframe, so a captured drone yields at most that sortie's traffic.
  4. No plaintext identifiers: platform ID and track IDs go inside the ciphertext (AeroScope lesson). Consider padding to fixed packet sizes to blunt traffic analysis.
  5. Data at rest: encrypt on-board logs and recordings with a per-sortie key, or don't store at all in SILENT mode unless needed. The laptop/C2 side holds the twin and needs disk encryption plus MDM-style device control if it touches Delta-like systems.
  6. Separate integrity from confidentiality for the control plane: if MinBand rides MAVLink, enable MAVLink 2 signing too.
- Do not invent crypto. Use libsodium, mbedTLS or the reference Ascon implementations. Expect government customers to ask for FIPS 140-3 validated modules, or national crypto (e.g., for Ukraine). This is an integration and certification cost, not an R&D problem.

### Gaps
- The MAVLink 2 signature size (believed to be 13 B: 1 B link ID, 6 B timestamp, 6 B truncated SHA-256) was not confirmed from a fetched primary source.
- Ukrainian national requirements for drone-link crypto (e.g., certified domestic algorithms) and NATO classification rules for track data were not found in public sources.
- No public detail on how Ukrainian ISR drones handle key zeroisation was found.

---

## Q7. Perception: which classes and sensors matter, at what ranges, with what datasets, and how do fielded systems handle false positives, human confirmation and "send a chip on demand"?

### Takeaway
Ukraine's operational AI (Avengers in Delta/Vezha) detects Russian military hardware: tanks, armoured vehicles, artillery, personnel, including concealed vehicles. The ministry claims about 70% detection and 2.2 s per object, retrained continuously on millions of DELTA frames. Thermal/IR matters at night. Decoys with thermal and radar signatures are widely used. Humans keep the final decision, and AI highlights or vets. MinBand's COCO classes ("person", "backpack") and RGB-only iPhone pipeline do not match this. A military taxonomy, thermal support, confidence calibration and image chips for human confirmation are needed.

### Cited Findings
- Avengers (MoD Centre for Innovation and Development of Defence Technologies): automatic detection and classification of enemy hardware in drone and fixed-camera video. Integrated with Delta's Vezha. Ministry claims more than 12,000 enemy targets detected per week (Sept 2024), about 70% of enemy equipment identified in streams, and an object detected in 2.2 s (2025). Continuously retrained for hard cases such as "tanks hidden in forests". All are self-reported. — [UNITED24 Media](https://united24media.com/defense-tech/what-can-ukraines-battlefield-ai-do-detect-russian-hardware-in-22-seconds-for-starters-22951); [UNITED24 (12,000/week)](https://united24media.com/latest-news/ukrainian-forces-use-ai-to-identify-12000-russian-targets-weekly-2549); [NV](https://english.nv.ua/nation/ukraine-uses-ai-tools-to-identify-russian-equipment-50453137.html)
- Vezha (Oct 2024) analysed feeds from more than 100 UAVs and could classify "more than 4,000 intelligence objects" (Chernohorenko). — [Militarnyi](https://militarnyi.com/en/news/vezha-streaming-module-is-now-available-in-delta-situational-awareness-system/)
- Avengers Labs is built on an annotated dataset of 5 million battlefield frames, mostly from DELTA. The Brave1 Dataroom (2026) gives more than 100 companies access to structured military datasets covering visual and thermal imagery across weather, time of day and sensor configurations. — [Search summary of Ukrainian MoD/Brave1 coverage, incl. Defence Industry Europe](https://defence-industry.eu/ukraine-says-more-than-70-ai-enabled-systems-are-striking-battlefield-targets-as-it-seeks-computer-vision-across-all-frontline-drones/)
- More than 70 AI-enabled systems are striking battlefield targets, more than 200 Ukrainian companies make AI-enabled drones, and Brave1 Market lists 46 AI solutions. The government aims to equip 100% of front-line drones with computer vision. — [Defence Industry Europe](https://defence-industry.eu/ukraine-says-more-than-70-ai-enabled-systems-are-striking-battlefield-targets-as-it-seeks-computer-vision-across-all-frontline-drones/)
- Enabled Intelligence released labelled Ukraine-conflict data including thermal-IR full-motion video ("half a million hours" of drone footage) for AI training (June 2026). — [DefenseScoop, 16 Jun 2026](https://defensescoop.com/2026/06/16/data-from-half-a-million-hours-of-ukraine-conflict-drone-footage-now-available-to-train-ai/)
- Public datasets: a Russia-Ukraine battlefield infrared dataset (about 3,000 UAV IR images; tanks, armoured vehicles, personnel, aircraft; Scientific Data, Jan 2024). A Roboflow RGB military vehicle dataset from recon drone imagery (CC BY 4.0). — [IEEE DataPort](https://ieee-dataport.org/documents/russia-ukraine-battlefield-infrared-dataset); [Roboflow Universe](https://universe.roboflow.com/militaryvehiclerecognition/military-vehicle-recognition)
- RGB-to-IR translation for vehicle detection in unseen UAV domains (2026 preprint) addresses the shortage of thermal training data. — [arXiv 2609.02556](https://arxiv.org/pdf/2609.02556)
- Johnson criteria (50% probability): detection 2 px, recognition 8 px, identification 12.8 px across the target's critical dimension. DJI's 79%-probability variant: 4/15/25 px. — [Elistair](https://elistair.com/?p=37011); [DJI Enterprise](https://enterprise-insights.dji.com/saraltitudeguideresult32)
- Thermal cameras on drones make night movement in the kill zone dangerous. — [NPR, Oct 2026](https://www.npr.org/2026/10/05/nx-s1-5925803/drones-buzz-overhead-troops-hunker-underground-in-ukraines-hellish-kill-zone)
- Decoys: inflatable vehicles with thermal and radar mimicry are used by both sides (e.g., InflaTech Leopard 2A4 decoys with IR/radar reflectors; Rusbal T-72 inflatables exposed by drone footage). — [Euromaidan Press / Forbes, Mar 2025](https://euromaidanpress.com/2025/03/04/forbes-ukraines-decoy-tanks-trick-russian-forces-next-active-dummies-will-spy-on-them/)
- Human-in-the-loop: Ukrainian officials stress a human keeps the final call. Interceptor terminal guidance highlights targets within about 2 km, then the operator commits. Brave1 testing of "last mile" modules. A contrary claim (opinion outlet) says an MoD adviser described future navigation, search and attack as fully autonomous. — [Euromaidan Press, Jun 2026](https://euromaidanpress.com/2026/06/25/ukraine-wants-an-ai-driven-army-its-new-defense-center-is-already-putting-ai-inside-kill-chain-steering-drones-onto-target-in-final-seconds/); [The Defender, May 2026](https://thedefender.media/en/2026/05/terminal-guidance-interceptor-drones/); [American Thinker, Sept 2026 (opinion)](https://www.americanthinker.com/articles/2026/09/when-russia-bombs-the-cloud-the-kill-chain-moves-onboard/)
- Delta lists "target vetting" among its functions. — [Wikipedia: Delta](https://en.wikipedia.org/wiki/Delta_(situational_awareness_system))
- Task-oriented feature transmission beat reconstruction-based semantic comms at very low SNR (97.57% vs 34.12% classification at -20 dB; satellite data, 2026 preprint). — [arXiv 2609.20150](https://arxiv.org/pdf/2609.20150)

### Inferences
- Taxonomy (engineering change): start with something like {person/dismount, small group, wheeled vehicle, tracked/armoured vehicle, artillery/towed gun, MLRS/large vehicle, EW/antenna mast, field fortification/trench, drone/launch site, unknown}. Use attributes (moving/static, hot/cold, possible-decoy) instead of exploding the class list. Default affiliation "unknown". MinBand should never assert "hostile".
- Thermal (engineering change): add an IR model path (fine-tune on the public IR dataset or translated RGB-to-IR data now; Brave1 Dataroom access later). Night ISR is where low-bandwidth semantic output helps most, because thermal video is also expensive to stream.
- Range arithmetic (inference): for a vehicle with about 2.1 m critical dimension (7 ft), recognition at 8 px needs about 0.27 m/px ground sample distance. For a person (about 0.38 m), recognition needs about 5 cm/px. At drone standoff ranges of 1-3 km, person recognition needs real zoom optics. A wide-FOV phone camera will only detect people at short range. Design for "detect then zoom/chip", not "classify everything from wide FOV".
- False positives and decoys (engineering change): (a) calibrated confidence plus track-level persistence (N frames, consistent motion) before a track is emitted; (b) a "possible decoy/low-thermal-consistency" flag where RGB and IR disagree; (c) human confirmation built into the protocol: entity state `UNCONFIRMED → OPERATOR_CONFIRMED / REJECTED`, with the operator's decision sent upstream (and downstream to suppress re-sends).
- Chip on demand (engineering change): keep a ring buffer of the best crop per track on the edge device. The operator requests `chip(track_id, size, quality)`, and the edge returns an encrypted JPEG/AVIF thumbnail in small chunks, scheduled by the budget controller. At 60 B/s a 2 KB chip takes about 33 s, so offer progressive (low-res first) chips. This is DARPA's "high-fidelity ROI" and the human-verification hook in one feature.
- Detection is done; geolocation, verification and integration are the hard parts. Ukraine already fields large-scale detection (Avengers). MinBand's differentiator is not "we detect tanks". It is "we deliver verified, geolocated tracks over links where video cannot go, into Delta/TAK".

### Gaps
- No public metrics on false-positive rates, decoy discrimination or operator workload for Avengers or other fielded systems.
- No public dataset specifically for decoys or camouflage in thermal imagery was found.
- Detection-range figures for specific fielded drone cameras (e.g., Mavic 3T, Shark's gimbal) were not collected.

---

## Q8. Edge hardware and SWaP: what compute is typical on drones, what power budgets apply, and what does porting from iPhone imply?

### Takeaway
Typical drone AI compute ranges from flight-controller-class ARM SoCs (Auterion Skynode S, shipping in tens of thousands to Ukraine) through Qualcomm QRB5165 (ModalAI VOXL 2, 16 g, 15 TOPS) to NVIDIA Jetson Orin Nano (20-67 TOPS at 7-25 W) and Hailo-8 accelerators (26 TOPS at about 2.5 W typical). DARPA's 2-5 W incremental budget excludes most Jetson configurations at full power. The iPhone's ARKit VIO, LiDAR and Neural Engine have no direct drone equivalent, so the port is a re-architecture of pose and depth, not just recompiling YOLO.

### Cited Findings
- ModalAI VOXL 2: Qualcomm QRB5165, 8 cores up to 3.091 GHz, 8 GB LPDDR5, 15 TOPS, 70 x 36 mm, 16 g. Supports PX4 (integrated flight controller). VOXL 2 Mini: 42 x 42 mm, 11 g. — [PX4: ModalAI VOXL 2](https://docs.px4.io/main/en/flight_controller/modalai_voxl_2.html); [ModalAI VOXL 2 Mini](https://www.modalai.com/en-kr/collections/voxl/products/voxl-2-mini)
- NVIDIA Jetson Orin Nano 8 GB: up to 40 TOPS, 7-15 W (newer "Super" datasheet: up to 67 TOPS, 7-25 W). 4 GB: up to 20 TOPS, 7-10 W. Module about 30 g, plus carrier, cooling and power. — [Jetson Orin Nano datasheet](https://static6.arrow.com/aropdfconversion/b4f120a8c52d5dd5e59875b129c4f94b77c7c3b6/jetson-orin-nano-datasheet-web.pdf); [Farnell listing](https://uk.farnell.com/nvidia/900-13767-0030-000/jetson-orin-nano-module-8gb-arm/dp/4200232)
- Hailo-8: up to 26 TOPS "consuming as little as 2.5 W", on-die memory, -40 to +85 C industrial grade. An M.2 module is listed at 2.5 W typical and 8.65 W max. Hailo-8L: 13 TOPS at 1.5 W typical (unconfirmed by datasheet). — [Mouser Hailo-8](https://www.mouser.com/new/hailo/hailo-hailo-8-ai-processor/); [Newark](https://www.newark.com/new-products/development-boards-evaluation-tools/hailo-8-ai-processor-family); [Waveshare](https://www.waveshare.com/directory/currency/switch/currency/CAD/uenc/aHR0cHM6Ly93d3cud2F2ZXNoYXJlLmNvbS9wcm9kdWN0L3Jhc3BiZXJyeS1waS9ib2FyZHMta2l0cy9oYWlsby04LWFjY2UtYS5odG0,/)
- Auterion: Skynode S onboard AI computer running AuterionOS. A US contract of about $50 M covers 33,000 AI guidance kits for Ukraine, with more than 50,000 planned. Nemyx swarm app runs on Skynode S. A separate report mentions an Auterion-related flight-control system with a Western ARM microprocessor costing about $18. — [The Defense Post, Sept 2025](https://thedefensepost.com/2025/09/05/auterion-nemyx-drone-swarm/); [Auterion](https://auterion.com/auterion-launches-nemyx-enabling-fully-coordinated-drone-swarms/); [DroneXL, Feb 2026](https://dronexl.co/2026/02/16/auterion-airlogix-ai-drone/); [Aeronaut.media](https://aeronaut.media/news-en/uavs-drones-news-en/en-ukraines-drones-get-ai-upgrades/)
- DARPA SA-ISR power: 5 W interim, 2 W final incremental, excluding camera and radio. — [DARPA](https://www.darpa.mil/research/programs/semantically-aware-isr)

### Inferences
- Porting map (engineering changes):
  - ARKit world tracking becomes VIO on the drone (VOXL 2 has a VIO pipeline; PX4/ArduPilot EKF with camera and IMU) plus the flight controller's GNSS/INS or visual-nav solution for the geodetic anchor (Q4).
  - LiDAR depth becomes LRF, DEM ray-cast or multi-view triangulation (iPhone LiDAR is a short-range indoor sensor).
  - Core ML / Neural Engine YOLO becomes TensorRT (Jetson), SNPE/QNN (QRB5165) or the Hailo Dataflow Compiler. Quantise to INT8 and benchmark at 2-5 W.
  - Printed marker origin becomes the platform's nav frame.
  - UDP over Wi-Fi becomes a pluggable transport (MAVLink tunnel, raw radio serial, IP over mesh).
- SWaP target: a Hailo-8 or VOXL 2-class setup fits DARPA's 2-5 W, while Jetson Orin Nano at 7-15 W does not. For a demo-to-prototype path, Jetson is fastest for development and Hailo/QRB5165 is closer to fieldable SWaP. The tracker, DR predictor and codec are cheap. The detector dominates power, so run detection at a low duty cycle (e.g., 2-5 Hz) and track in between.
- Keep the core (tracker, predictor, codec, crypto) as portable C/C++ or Rust with no iOS dependencies, so the same code runs on the laptop twin, an ARM Linux companion computer and potentially an AuterionOS app. Fielded deployment via an AuterionOS app or a VOXL "MPA" service could be a fast route to many Ukrainian airframes, given Auterion's installed base.
- Environmental hardening (temperature range, vibration, EMI next to radios) is a real gap for any phone-based demo.

### Gaps
- VOXL 2 power draw in watts was not found.
- Skynode S compute specs (SoC vendor, TOPS) were not confirmed. No Qualcomm link was found.
- Whether third parties can deploy apps on AuterionOS in Ukrainian units, and under what terms, is unknown.

---

## Q9. Multi-drone: relays, swarm data sharing, multi-platform track fusion, and time sync without GNSS

### Takeaway
Relay drones and masts are already routine in Ukraine (tens to more than 100 km extension), and mesh radios (Doodle/Silvus) and swarm stacks (Auterion Nemyx) assume multi-node networks. To be useful across platforms, MinBand needs globally unique track IDs, a shared timebase, geodetic coordinates and covariance-aware track-to-track fusion. Research shows time-sync error directly degrades association and fusion. GNSS-free time transfer is still an active research area.

### Cited Findings
- Relays and repeaters (see Q1): Brave1 tested more than 10 Ukrainian repeater makers. Airborne and mast repeaters claim about 14-25 km each. A tethered aerostat repeater reaches about 20 km at 1 km height for up to 4 hours. Russia's Odyssey relay claims about 70 km with relay-frequency switching. — [Militarnyi](https://militarnyi.com/en/news/brave1-tests-tools-to-increase-drone-communication-range/); [BlueBird](https://www.blue-bird.tech/en/products/promin-13-drone-repeater-for-fpv-bluebird-tech/); [The Defense Post, May 2026](https://thedefensepost.com/2026/05/22/russia-airborne-relay-system/)
- Doodle Labs Mesh Rider and Silvus StreamCaster are MANET (mesh) radios. — [Doodle Labs products](https://doodlelabs.com/products/); [Commercial UAV News](https://www.commercialuavnews.com/silvus-technologies-unveils-streamcaster-lite-5200-ultra-low-swap-oem-module-delivering-powerful-manet-radio-performance-for-leading-edge-unmanned-systems)
- Auterion Nemyx lets drones from different manufacturers running AuterionOS/Skynode S act as one AI-coordinated swarm. — [Auterion](https://auterion.com/auterion-launches-nemyx-enabling-fully-coordinated-drone-swarms/)
- Time-sync error degrades multi-sensor track association and filtering, per a track-to-track fusion benchmark modelling clock holdover drift (Lee et al., Drones 2024). — [Drones 8(5):167](https://www.mdpi.com/2504-446X/8/5/167/review_report)
- GNSS-free sync methods: Doppler-plus-timestamp distributed Kalman clock tracking for high-dynamic multi-UAV networks (IEEE TII 2025). Consensus time sync with convergence proof (IEEE TWC 2024). Two-way time transfer, which assumes symmetric delays that are hard to meet on UAS. AFRL work targets about 100 ps sync for distributed RF on UAS without GPS. — [BIT: distributed clock tracking](https://pure.bit.edu.cn/en/publications/distributed-clock-parameter-tracking-for-highly-dynamic-multi-uav/); [BIT: consensus sync](https://pure.bit.edu.cn/en/publications/a-novel-consensus-based-distributed-time-synchronization-algorith/); [USPTO 11,864,140](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/11864140); [UNM seminar (AFRL)](https://ece.unm.edu/news/2024/04/april-26-seminar-khanh-pham.html)
- GNSS timing degrades in GNSS-denied environments. A ToF plus channel-impulse-response Bayesian sync architecture beat consensus baselines in simulation. — [Remote Sensing 17(22):3715](https://www.mdpi.com/2072-4292/17/22/3715)
- DIS Entity State PDU minimum is 144 bytes (KDIS). The NPS data dictionary lists 1,280 bits (160 B), a conflict probably due to field counting or revision. — [KDIS](https://kdis.sourceforge.net/classdoc/_entity___state___p_d_u_8h_source.html); [NPS DIS dictionary](https://faculty.nps.edu/brutzman/vrtp/mil/navy/nps/disEnumerations/JdbeHtmlFiles/pdu/29.htm)
- Multi-UAV semantic communication research models quantised semantic bits with per-device bandwidth allocation. — [arXiv 2609.14476](https://arxiv.org/pdf/2609.14476); [arXiv 2601.01430](https://arxiv.org/html/2601.01430v1)

### Inferences
- Engineering changes:
  1. IDs: `track_uid = (platform_id, sortie_epoch, local_track_id)`, with a fusion-layer `fused_id`. CoT uid derives from it.
  2. Timebase: every update carries a timestamp in a shared timebase. Use GNSS time when trustworthy, otherwise a sync protocol over the mesh (two-way exchange piggybacked on MinBand packets, or the radio's own TDMA timing). Also carry an estimated clock-uncertainty term that inflates the position covariance (velocity x clock error). DR across platforms is only as good as clock agreement: at 15 m/s, 200 ms of clock error is 3 m.
  3. Fusion: covariance intersection or track-to-track fusion at the C2 node (or on a relay drone). Without geodetic coordinates and covariances (Q4), cross-platform fusion is impossible. Q4 is a prerequisite.
  4. Relay-aware transport: store-and-forward with de-duplication (the same entity update arriving via two relays). Hop-count/TTL. Relays aggregate and compress (e.g., re-cluster) rather than forward blindly, since the bottleneck is usually the deepest hop.
  5. Positioning vs. DIS: MinBand's per-entity delta should be much smaller than a 144-B DIS ESPDU. Quote that comparison, but say clearly that DIS is a simulation interoperability standard, not a field ISR standard.
- Multi-platform "world twin" is a strong pitch for European C2 and drone-wall concepts, which emphasise interoperable sensor networks. But EDDI/EFW have no published data standard, so CoT plus MISB remain the safe outputs.

### Gaps
- No public detail on how Ukrainian units fuse tracks from multiple drones (e.g., inside Delta) or on what timebase they use under GNSS jamming.
- The IEEE 1278.1 dead-reckoning thresholds and 5 s heartbeat (commonly cited) were not confirmed from primary text.
- No field data on relay-chain latency or loss per hop was found.
