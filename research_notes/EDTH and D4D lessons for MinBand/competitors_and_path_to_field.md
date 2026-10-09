# MinBand: competitive landscape and path from hackathon to fielded capability (Europe and Ukraine, as of 2026-10-09)

Method note for the report writer: research was done on 2026-10-09 with web search. Direct page fetches were blocked by the session's egress proxy for most primary domains (darpa.mil, helsing.ai, auterion.com, brave1.gov.ua, diana.nato.int, thedefender.media, bwcoconsulting.com). Where a fact below cites one of those URLs, the content came from the search engine's extract of that page, not from reading the page myself. Treat exact numbers from those pages as "reported, verify before quoting". Vendor performance figures are labelled as company claims. GitHub URLs seen in results are recorded but were not opened, per the constraint.

MinBand reference numbers used for the comparisons (from the brief, not independently verified): 60–800 B/s (0.48–6.4 kbit/s) of entity state deltas, compared with about 1.5 Mbit/s for H.264, in synthetic evaluations.

---

## 1. Competitors and adjacent products: who already sends detections or tracks instead of video, and with what bandwidth claims?

### Takeaway
No European, Ukrainian or US vendor I found publishes a bandwidth figure for a "tracks/entities only" downlink from small drones. The big players (Helsing Altra, Anduril Lattice, Palantir Maven, Auterion, Shield AI) all use an entity or track data model and onboard autonomy. Their public material is about autonomy that survives link loss (terminal guidance, swarming, evasion), not about a semantic downlink with byte budgets. Ukraine's national stack (DELTA, Vezha, Avengers) is built around video: more than 100,000 drone video streams a month go to a central AI. That leaves room for an OEM-neutral "semantic link" layer. The closest existing building blocks are open standards and open-source tools (MISB ST 0903 VMTI, TAK Cursor-on-Target in protobuf form, the Meshtastic TAKPacket SDK), not products.

### Cited Findings

**Primes and platform software (Europe and US)**

- **Helsing, Altra and HX-2.**
  - Altra is Helsing's recce-strike software. Per the company page, it integrates "live data streams from ISR drones, spotters and other sources" into a common operational target picture. It also offers AI-aided effector assignment, automated fire adjustment, coordinated HX-2 strike swarms, and "onboard autonomy for unmanned strike and ISR operations in heavily EW-contested environments" (company marketing). [Helsing Altra](https://helsing.ai/altra)
  - At DSEI in September 2025, Helsing and Systematic announced that Altra would be integrated with Systematic's SitaWare C4ISR. [Janes](https://www.janes.com/defence-intelligence-insights/defence-news/security/dsei-2025-helsing-and-systematic-partner-on-swarming-recce-strike-c2-system)
  - Helsing is producing 6,000 HX-2 strike UAVs for Ukraine. Janes reports that HF-1 and HX-2 are resistant to GNSS jamming. [Janes](https://www.janes.com/defence-intelligence-insights/defence-news/air/ukraine-and-helsing-expand-ai-co-operation-development); [Yahoo/DPA](https://www.yahoo.com/news/german-company-manufacture-6-000-051134045.html)
  - On 13 July 2026 Helsing raised a $1.8bn Series E at an $18bn valuation. Investors included General Catalyst, Plural, Lightspeed and Dragoneer. [Helsing press release](https://helsing.ai/newsroom/helsing-raises-1-8bn-in-series-e); [Grosswald](https://www.grosswald.org/helsing-1-8-billion-series-e-18-billion-valuation-europe-largest-defence-tech-round/)
  - On 25 February 2026 the Bundestag budget committee approved a loitering-munition framework of about €540m, split roughly €269m each between Helsing (HX-2) and Stark (Virtus). [Grosswald](https://www.grosswald.org/stark-defence-500-million-series-c-sequoia-founders-fund-loitering-munition/); [DroneXL](https://dronexl.co/2026/02/25/germany-helsing-stark-kamikaze-drone-deal)
  - I found no public bandwidth figure for Altra or HX-2 telemetry or ISR feeds, and no source saying Altra is integrated with Ukraine's DELTA. [Search summary of Helsing/Delta coverage](https://www.janes.com/defence-intelligence-insights/defence-news/air/ukraine-and-helsing-expand-ai-co-operation-development)

- **Quantum Systems (Vector, Twister, Sparta, Falke).**
  - The Vector AI tech sheet lists "AI Processing 2 x NVIDIA Jetson Orin", a data link range of 60+ km, AES-256, and 2.2–2.5 and 4.4–4.9 GHz bands. [QS Vector AI Techsheet (May 2025)](https://lp.quantum-systems.com/hubfs/Downloadables/Downloadables%20GmbH/QS_VectorAI_Techsheet_250502_Screen.pdf?hsLang=en)
  - Its upgrade kit uses a Jetson Orin "for better object detection, classification, identification and tracking". [Defence Connect](https://defenceconnect.com.au/industry/14405-quantum-systems-confirms-artificial-intelligence-drone-upgrades-deployed-to-ukraine)
  - The company markets "real-time, high-resolution downlink imagery". I found no source saying Vector sends detection metadata instead of video. [Militär Aktuell](https://militaeraktuell.at/en/quantum-systems-reconnaissance-drone-with-anti-interception-protection/)
  - July/August 2026: a camouflage-evasion AI on Vector performs evasive manoeuvres on its own when it detects a hunter drone. [DroneXL](https://dronexl.co/2026/08/05/quantum-systems-vector-yellow-camouflage-evasion-ai/)
  - "Sparta" is a carrier drone (up to 9 kg payload, 8 h endurance), not a software platform. Source: a March 2026 weekly digest (lower reliability). The same digest reports in-country Twister production in Ukraine and a 15,000-unit STRILA-2 interceptor deal with WIY Drones. [Robotics.press digest](https://www.robotics.press/news/quantum-systems-ukraine-air-defense-pivot/)
  - Twister was chosen as the Bundeswehr ALADIN successor (May 2025). [defence-industry.eu](https://defence-industry.eu/tag/twister/)
  - A reported order of 520 Falke surveillance systems for €210m, with delivery in 2026. [Keen Venture Partners guide](https://www.keenventurepartners.com/posts/europes-biggest-defence-budget-also-one-of-the-hardest-to-reach-keen-venture-partners-publishes-the-founders-guide-to-german-defence-procurement)
  - Quantum Systems drones are orderable on Brave1 Market and DOT-Chain Defence. [Militarnyi](https://militarnyi.com/en/news/quantum-systems-drones-now-available-on-brave1-market-and-dot-chain-marketplaces/)
  - Quantum Systems acquired Fernride (ground autonomy software) in December 2025. [Munich Startup](https://munich-startup.de/news/quantum-systems-uebernimmt-fernride)
  - In April 2025 it partnered with the Ukrainian startup Frontline to integrate Frontline products into Vector and Twister and the "Drone Port" infrastructure "for real-time autonomous aerial reconnaissance". [AIN.ua](https://en.ain.ua/2025/04/15/frontline-agreement-with-quantum-systems); [Defence Matters](https://defencematters.eu/ukrainian-drone-maker-frontline-signs-strategic-agreement-with-quantum-systems/)

- **Auterion (Skynode S, AuterionOS, Nemyx, strike kits).**
  - Nemyx is swarm software delivered as an app on AuterionOS and optimised for Skynode S. Auterion claims "over 50,000 Skynode S units in service" and that any compatible drone can join a swarm through a software upgrade (company claim). [Auterion Nemyx](https://auterion.com/product/nemyx/)
  - Nemyx launched in September 2025. [The Defense Post](https://thedefensepost.com/2025/09/05/auterion-nemyx-drone-swarm)
  - On 13 January 2026, in a Swarm Forge-linked live fire at Camp Blanding, one operator directed three EFP-armed drones against three targets. [Inside Unmanned Systems](https://insideunmannedsystems.com/auterion-demonstrates-one-operator-drone-swarm-strike-in-u-s-live-fire-test/)
  - Auterion has a Pentagon-funded deal for 33,000 AI strike kits for Ukraine. [search summary, DroneXL profile](https://dronexl.co/de/drone-companies/auterion/)
  - In July 2026 Auterion and SkyFall agreed to ship 50,000 Shrike FPVs fitted with Auterion strike kits (Skynode S, CV terminal guidance), worth about €90m. Reuters/Kyiv Post name Germany as funder; Breaking Defense says "an undisclosed European NATO country". [Auterion](https://auterion.com/auterion-and-skyfall-to-ship-50000-shrike-strike-drones-to-ukraines-front-lines/)
  - Auterion's Skynode S connectivity documentation splits links into "IP based high-bandwidth links" (which carry video) and "serial (MAVLink) low-bandwidth links" (which do not carry video and terminate at the FMU). The low-bandwidth path is therefore control and telemetry only. [Auterion docs: Skynode S connectivity](https://docs.auterion.com/hardware-integration/skynode-s/connectivity)
  - AuterionOS ISR (February 2025) supports multicast video to multiple GCS and ATAK devices to reduce datalink load, KLV metadata recording, and a CoT marker manager. The Skynode S page lists "bidirectional communication with TAK via CoT". [Auterion ISR update](https://auterion.com/enhanced-isr-capabilities-video-flight-systems-and-navigations-and-user-experience/); [Skynode S](https://auterion.com/product/skynode-s/)
  - The Auterion SDK has a Visual Tracking API. [Auterion docs](https://docs.auterion.com/app-development/auterion-sdk/visual-tracking-api)
  - Auterion and Helsing both market themselves as ITAR-free. [Times report reproduced](https://scribe.disroot.org/post/2523894)

- **Shield AI (Hivemind, EdgeOS).**
  - Hivemind is designed to "run fully on the edge, disconnected from the cloud, in high threat, GPS and communication-degraded environments". [Shield AI release, 11 Feb 2026](https://shield.ai/shield-ai-signs-contract-with-taiwans-national-chung-shan-institute-of-science-and-technology-to-accelerate-and-indigenize-taiwan-developed-ai-pilots/)
  - EdgeOS is described as the on-vehicle OS layer by a third-party profile. [Robotics.press](https://www.robotics.press/companies/shield-ai/)
  - Shield AI raised $2bn at a $12.7bn valuation (date not shown in the excerpt). [TNW](https://thenextweb.com/news/shield-ai-2-billion-hivemind-autonomous-defence)
  - I found no Shield AI material on downlink compression or semantic links.

- **Anduril (Lattice, Lattice Mesh, Lattice SDK).**
  - The Lattice SDK guidance is "Local-first … develop with bandwidth utilization in mind, and build with the assumption that the network conditions … can be intermittently unavailable". [Anduril developer docs: principles](https://developer.anduril.com/guides/concepts/principles)
  - The SDK exposes gRPC Entity Manager and Task Manager APIs. Sandbox access goes through a gated "Lattice Developer Program" for "qualified developers". [Anduril docs overview](https://docs.anduril.com/guide/overview)
  - CDAO awarded Anduril about $100m to scale a Lattice-powered "Edge Data Mesh". [Inside Defense](https://insidedefense.com/node/222723); [Unmanned Airspace](https://www.unmannedairspace.info/counter-uas-systems-and-policies/dod-cdao-awards-production-agreement-to-anduril-to-deliver-edge-data-mesh/)
  - An Anduril job post describes a "Distributed Service Bus" doing multi-path routing over tactical radios and SATCOM with pub/sub and gRPC proxying. [Greenhouse job post](https://job-boards.greenhouse.io/andurilindustries/jobs/5092819007)
  - European presence: Rheinmetall partnerships. The first was a June 2024 C-sUAS MoU (Lattice with Skymaster). The second, in June 2025, covers European variants of Barracuda and Fury integrated into Rheinmetall Battlesuite. [Rheinmetall 2024](https://www.rheinmetall.com/en/media/news-watch/news/2024/06/2024-06-19-rad-and-anduril-industries-sign-mou); [The Register 2025](https://www.theregister.com/2025/06/18/anduril_rheinmetall_drones/)

- **Palantir (Maven Smart System, MetaConstellation).**
  - NATO ACO acquired "MSS NATO", finalised on 25 March 2025 after about a six-month procurement. [Breaking Defense](https://breakingdefense.com/2025/04/nato-picks-palantirs-maven-ai-for-military-planning-amid-trans-atlantic-tension/)
  - A July 2026 report says it was deployed for eastern-flank surveillance (secondary, Russian-language source). [Moscow Times (RU)](https://ru.themoscowtimes.com/2026/07/09/nato-doverilo-slezhku-za-vostochnoi-granitsei-ii-sisteme-ot-amerikanskoi-palantir-a200396)
  - Reported Ukraine results were "mixed". [AIN.ua](https://en.ain.ua/2025/04/15/nato-acquires-palantir-military-ai-system)
  - Maven is an enterprise or theatre-level fusion system. I found no primary source on MetaConstellation's current role.

- **Tekever.**
  - Tekever became a unicorn in 2025, alongside a £400m UK investment programme ("OVERMATCH"). [dev.ua](https://dev.ua/en/news/tekever-iedynoroh-1746619925); [CMU Portugal](https://cmuportugal.org/usof)
  - The AR3 has logged 10,000+ flight hours in Ukraine. Onboard AI/ML is attributed to the ARX platform, and the software layer is ATLAS, an "AI-powered intelligence and mission-management platform". I found no bandwidth claims. [Tectonic](https://www.tectonicdefense.com/icymi-tekever/); [Robotics.press](https://www.robotics.press/companies/tekever/)

- **Stark (Virtus).**
  - Unicorn in January 2026; raised about €500m ($570m) in June 2026 with Sequoia and Thiel/Founders Fund named. [The Defense Post](https://thedefensepost.com/2026/06/29/stark-funding-expand-defense-manufacturing/); [DroneXL](https://dronexl.co/2026/06/24/thiel-sequoia-570m-german-drones/)
  - DroneXL reports that Virtus missed in four attempts during October 2025 UK/German trials. [DroneXL](https://dronexl.co/2025/11/03/germany-awards-950m-drone-contract)

- **Tytan Technologies.** €30m Series A in February 2026, co-led by the NATO Innovation Fund and Armira, with Lakestar, Visionaries, OTB, D3 and others. Its product is the METIS AI interceptor that "detect[s], track[s], and neutralise[s]" drones. It has contracts with the German and Ukrainian armed forces. [NIF](https://www.nif.fund/news/the-nato-innovation-fund-co-leads-e30m-series-a-for-tytan-to-build-europes-next-generation-air-defence/)

- **Delian Alliance Industries (Athens/London).**
  - $14m Series A, led by Air Street and Marathon (announced August 2025). [Osborne Clarke](https://www.osborneclarke.com/news/osborne-clarke-advises-delian-alliance-industries-14m-series-investment)
  - Products are autonomous aerial and sea drones and surveillance towers. The Greek army uses them for sea-border monitoring around islands, a dual-use or border-surveillance precedent. [newmoney.gr](https://www.newmoney.gr/roh/palmos-oikonomias/epixeiriseis/delian-alliance-industries-i-eteria-pou-echi-vali-tin-ellada-ston-evropaiko-charti-tis-amintikis-kenotomias/)

- **ARX Robotics (Mithra OS).**
  - €31m Series A in April 2025 (HV Capital, NIF, Project A, Omnes), plus about €11m extension (Speedinvest). Mithra OS retrofits vehicles for autonomy. [Tech.eu](https://tech.eu/2025/04/28/arx-robotics-raises-eur31m-series-a-to-advance-military-automation/); [Vestbee](https://vestbee.com/blog/articles/arx-robotics-raises-11-m)
  - In October 2026 ARX stepped up UGV collaboration with Ukraine's Roboneers. [Tech.eu](https://tech.eu/2026/10/04/arx-robotics-and-roboneers-step-up-ugv-collaboration-for-ukraines-battlefield/)
  - Unverified negative claims (UK facility cancelled) appear only on a jobs-profile page; treat as unconfirmed. [Robotics.press](https://www.robotics.press/news/arx-robotics-six-nation-european-deployment/)

- **Origin Robotics (Latvia, BLAZE interceptor).**
  - Received €4.5m from the EDF in 2024 for unmanned target designation work. [Search summary](https://eng.lsm.lv/article/society/defence/06.02.2026-latvia-estonia-and-belgium-receive-first-blaze-military-drones.a633457/)
  - Latvia approved the first contract under a multi-year framework on 21 April 2026, financed via EU SAFE. The framework is open to other European countries. [Janes](https://www.janes.com/defence-intelligence-insights/defence-news/air/latvia-orders-more-blaze-interceptor-uavs-under-framework-agreement-with-origin-robotics)
  - Deliveries to Latvia, Estonia and Belgium began in early 2026. France reportedly chose BLAZE (Eurosatory 2026). [EDR](https://www.edrmagazine.eu/estonia-latvia-and-belgium-take-delivery-of-blaze-interceptor-drones-from-origin-robotics); [LV Portals](https://lvportals.lv/dienaskartiba/391239-latvijas-aizsardzibas-tehnologijas-gust-starptautisku-atzinibu-francija-izvelas-latvija-izstradatu-partverejdronu-sistemu-2026)

**Ukrainian software, autonomy modules and links**

- **Swarmer (Styx AI).**
  - One operator controls many drones, and "drone manufacturers also license Swarmer's software for integration with their hardware". This is the closest existing precedent for an OEM-licensed software layer. $15m Series A in September 2025. [DroneXL](https://dronexl.co/2025/10/13/ukrainian-drone-swarm-startup-swarmer)
  - Filed with the SEC for a Nasdaq IPO on 2 February 2026. A post-IPO report gives about $374m market cap, 2025 revenue of $309,920 (mostly one OEM contract), and a revenue drop after losing a key Ukrainian partner. Single-source; verify against SEC filings. [Euromaidan Press](https://euromaidanpress.com/2026/02/04/ukrainian-swarmer-startup-raised-largest-defense-investment-since-war-began-now-its-filing-for-worlds-second-largest-exchange-in-new-york); [dev.ua](https://dev.ua/en/news/pershyi-finansovyi-zvit-swarmer-pislia-ipo-1778840282)

- **NORDA Dynamics (Lviv).**
  - Products: "Underdog" last-mile CV autonomy (the pilot selects a target, the drone completes the approach) and StableLink, GPS-free position hold for repeaters.
  - Company claims: "integrated with dozens of Ukrainian drone manufacturers" and a headline of 10,000 licences sold. $1m seed in September 2025 led by Varangians. [DroneXL](https://dronexl.co/2025/09/25/ukraine-drone-norda-dynamics-gps-free-flight); [Ukraine's Arms Monitor](https://ukrainesarmsmonitor.substack.com/p/towards-greater-drone-autonomy-norda)

- **The Fourth Law (TFL-1 autonomy module).** Used by more than 50 Ukrainian units (company claim). Raised investment from Axon in February 2026. [Kyiv Post](https://www.kyivpost.com/amp/post/70214); [Ukrainska Pravda](https://www.pravda.com.ua/eng/news/2026/02/16/8021260)

- **Sine.Engineering.** Builds a multi-band C2 module and time-of-flight (non-GPS) navigation. Claims more than 50 drone makers use its products (company claim, unverified). [AOL/Business Insider](https://www.aol.com/ukrainian-tech-company-working-beat-113702269.html)

- **Rise Technologies (Nakande link).** August 2026 report: 160 g airborne module with frequency hopping across up to 1,000 MHz, Ethernet and MAVLink, combined control, telemetry and video, downlink up to 5.2 Mbit/s, and a quoted 118 km range (maker claims). I found nothing on a product called "PALYCH". [Euromaidan Press](https://euromaidanpress.com/2026/08/21/160-grams-118-km-link-and-jam-resistance-by-frequency-hopping-ukraines-answer-to-drone-wars-connectivity-problem/)

- **Himera (G1 handheld radio).** Encrypted frequency hopping; a jam-resistant ground radio, not a drone link. [Janes](https://www.janes.com/defence-news/news-detail/ukraine-conflict-ukraine-develops-jam-resistant-radio)

- **Mesh networking need.** Serhii "Flash" Beskrestnov, adviser to the defence minister, said in April 2026 that Ukraine needs its own encrypted, EW-resistant mesh modems for UAV control at 300–400 km "with enough bandwidth for stable HD video", because "Western modems are too expensive" and Chinese ones cannot be relied on. [Euromaidan Press](https://euromaidanpress.com/2026/04/29/ukraine-needs-to-build-its-own-high-speed-mesh-tech-as-china-is-russias-supplier-and-western-modems-are-too-expensive/)

- **SkyFall (Vampire, Shrike, P1-SUN).** P1-SUN interceptor got an integrated AI module for autonomous detection in June 2026. Localising away from Chinese parts. [Mind.ua](https://mind.ua/news/20300885-skyfall-pogliblyue-lokalizaciyu-droniv-vampire-i-shrike); [Militarnyi](https://militarnyi.com/en/news/ukrainian-shrike-10-fpv-drone-used-to-shoot-down-russian-mi-28/)

- **Vyriy Drone.** Reported 70% localisation by mid-2024. In December 2024 it built a domestic-component FPV that still used some Chinese magnets, lenses and chips. Later "100% Ukrainian" claims are unverified. No AI or semantic-link product found. [Euromaidan Press](https://euromaidanpress.com/2024/12/20/ukrainian-company-assembles-first-fully-domestic-fpv-drone/)

- **Griselda.**
  - An AI intelligence-fusion system built by Brave1 cluster members. It collects from satellites, drones, social media and other sources, and claims 28 seconds from ingest to delivery.
  - Integrated into DELTA and into Kropyva, Armor/Bronya, Ukrop and GisArta.
  - Raised $600k from Double Tap (March 2025). [mil.in.ua](https://mil.in.ua/en/news/ukraine-develops-intelligence-system-based-on-artificial-intelligence); [AIN.ua](https://en.ain.ua/2025/03/07/griselda-raises-600000)

- **Kropyva.** Android tablet fire-control and situational-awareness app built by Army SOS volunteers in 2014. [Ukrainian GUR](https://gur.gov.ua/en/content/kropyva-diie-vluchno); [mil.in.ua](https://mil.in.ua/en/news/ukraine-develops-intelligence-system-based-on-artificial-intelligence)

- **DELTA, Vezha and Avengers (Ukrainian MoD).**
  - DELTA is the MoD situational-awareness and battle-management system. [Wikipedia](https://en.wikipedia.org/wiki/Delta_(situational_awareness_system)); [CSIS](https://www.csis.org/analysis/does-ukraine-already-have-functional-cjadc2-technology)
  - Vezha is DELTA's video streaming and analysis module. An 8 October 2026 report covers Vezha streaming integrated into DELTA with DELTA authentication. [Militarnyi](https://militarnyi.com/en/news/vezha-streaming-module-is-now-available-in-delta-situational-awareness-system/)
  - Avengers, built by the MoD Innovation Center, runs on Vezha video. In 2024 it detected about 12,000 vehicles per week. [MoD Ukraine (2024)](https://mod.gov.ua/en/news/12-000-enemy-targets-are-detected-by-the-ukrainian-military-weekly)
  - Per an August 2026 MoD release reported by Janes and The Defense Post, automated detection now processes more than 100,000 UAV video streams per month and detects about 70% of targets in real time. Avengers Labs offers industry 5 million annotated frames, mostly from DELTA. [The Defense Post](https://thedefensepost.com/2026/08/12/ukraine-ai-drone-target-detection/amp/); [Janes](https://www.janes.com/defence-intelligence-insights/defence-news/security/uk-considering-using-ukraines-avengers-labs-data-for-target-identification)
  - The UK and Ukraine signed an AI partnership on 24 August (2026) giving access to Avengers Labs data. [Janes](https://www.janes.com/defence-intelligence-insights/defence-news/security/uk-considering-using-ukraines-avengers-labs-data-for-target-identification)
  - A "Mission Control" UAV mission-planning module was added to DELTA in January 2026. [Rubryka](https://rubryka.com/en/2026/01/23/systemu-upravlinnya-bpla/); [Defender Media](https://thedefender.media/en/2026/01/mission-control-launch/)
  - DELTA was tested for allied interoperability at NATO CWIX24 and integrated with Poland's TOPAZ. [NATO ACT](https://www.act.nato.int/article/delta-system-cwix/); [ArmyInform](https://armyinform.com.ua/2024/07/01/ukrayinska-delta-poyednalasya-z-polskym-topaz/)

**Standards and open-source tools MinBand could plug into**

- **MISB ST 0903 (VMTI).** The NATO/US metadata standard for "tracks and associated indicators of motion" in motion imagery: target ID, track history, geolocation, confidence and class. It can travel inside ST 0601 KLV or "as a 'stand alone' stream, independent of any motion imagery".
  - Tooling: Impleo MisbCore SDK (commercial) and the `pymisb` Python package.
  - [Impleo ST 0903 guide](https://www.impleotv.com/content/misbcore/help/ST903/st903.html); [pymisb on PyPI](https://pypi.org/project/pymisb/); [Impleo STANAG 4609 overview](https://impleotv.com/2025/03/11/stanag-4609-isr-video/)
- **TAK and Cursor-on-Target.**
  - Typical CoT XML messages are "a few hundred bytes". TAK Protocol v1 uses protobuf (`TakMessage`), with Mesh SA (UDP) and stream framings. An Isode white paper on operating TAK over HF radio was published in May 2026. [Isode whitepaper](https://www.isode.com/wp-content/uploads/2026/05/Operating-TAK-over-HF-Radio.pdf); [takproto docs](https://takproto.readthedocs.io/en/latest/tak_protocols/)
  - The Meshtastic TAKPacket SDK (V2) uses protobuf with zstd dictionaries. Its own docs report CoT XML of 400–2,300 B compressing to a median 87 B (max 184 B) per packet over LoRa. [Meshtastic TAK protocol](https://meshtastic.org/docs/software/apple/developer/tak-protocol/); [TAKPacket SDK](https://klibs.io/project/meshtastic/TAKPacket-SDK)
- **FreeTAKServer and FreeTAKUAS.** FreeTAKServer is a Python TAK server. FreeTAKUAS is a DJI app that pushes drone position, sensor point of interest and FOV as CoT via the FTS REST API and includes TensorFlow Lite object detection. Its GitHub topic listing says "Looking for a maintainer!". GitHub (not opened): `https://github.com/FreeTAKTeam/FreeTAKUAS`. [PyPI FreeTAKServer](https://www.pypi.org/project/FreeTAKServer/0.1.9/); [OpenSourceForU](https://www.opensourceforu.com/2022/09/the-tak-ecosystem-open-source-military-coordination/)
- **OpenTAKServer.** A pure-Python open-source TAK server. No drone-specific plugin found, but it accepts standard CoT. [Knogin TAK ecosystem page](https://knogin.com/en/developers/tactical-awareness-tak-ecosystem)
- **DragonSync and AryaOS.** Both convert RF drone detections (Remote ID, DJI DroneID) into native CoT tracks for ATAK, WinTAK or iTAK and TAK Server. This is a precedent for "detections as CoT", but for RF detection, not EO tracking. GitHub (not opened): `https://github.com/alphafox02/DragonSync`. [AryaOS docs](https://aryaos.readthedocs.io/en/latest/deploy/counter-uas/)
- **UAS Tool.** A free ATAK plugin, distributed via COTAK, for flying common drones and sharing their position and video. [COTAK](https://cotak.gov/pages/news/uas-tool-training-videos)
- **Dronecode stack.** MAVLink, MAVSDK and PX4 are the open interoperability baseline for drone OEMs. [Dronecode](https://dronecode.org/projects/)

**Academic "semantic communication for UAVs" (prior art to cite or differentiate)**
- arXiv:2502.03761 (February 2025): knowledge-graph semantic compression for UAV object detection under low bandwidth and SNR. [arXiv](https://arxiv.org/abs/2502.03761v1)
- arXiv:2601.01430 (January 2026): context-aware digital semantic communication in UAV networks. [arXiv](https://arxiv.org/abs/2601.01430v2)
- LPUSC (JEIT, 2026): object-level and semantic-region UAV image transmission. [JEIT](https://www.jeit.ac.cn/en/article/doi/10.11999/JEIT260370)
- These works focus on learned feature or image reconstruction (JSCC), not on symbolic entity state with shared dead-reckoning.

### Inferences
- **Nobody publishes a track-only downlink budget.** No competitor found publishes a bytes-per-second figure for a track-only downlink. Every public bandwidth number is for video links: QS 60+ km link, Rise Nakande up to 5.2 Mbit/s, Beskrestnov's "stable HD video" goal. MinBand's 60–800 B/s is two to four orders of magnitude below the video links buyers are paying to harden. That is the core of the pitch.
- **Ukraine's national architecture is video-up, AI-in-the-rear.** Avengers runs centrally on more than 100,000 Vezha video streams a month. MinBand does the inverse: AI at the edge, state up. It complements rather than competes, but integration into DELTA or Vezha is the adoption bottleneck in Ukraine. One realistic route: emit tracks that DELTA's "Monitor" layer can ingest, and offer on-demand video chips through Vezha when the link allows.
- **Autonomy players solve a different problem.** Helsing, Auterion (Nemyx, strike kits), NORDA, TFL and Swarmer solve "keep killing when the link dies" with terminal guidance and swarming. MinBand solves "keep the commander's picture current when the link is thin". These are complementary. MinBand is a natural app on AuterionOS or Skynode, or a sidecar to Swarmer or TFL modules, rather than a rival.
- **The low-bandwidth MAVLink link is where MinBand fits on Auterion.** Auterion's docs show the low-bandwidth serial MAVLink path carries no video. A MinBand entity stream sized for that path (a custom MAVLink message or tunnel) would add ISR value exactly where Auterion currently offers none. This is a hypothesis to check with Auterion's SDK.
- **The real prior-art competitor is open standards.** MISB ST 0903 VMTI plus TAK CoT/protobuf (and Meshtastic's 87 B median packets) already provide "detections as metadata". MinBand's defensible novelty is:
  - shared deterministic predictor and send-on-error (DIS-style dead reckoning applied to vision tracks);
  - a budget controller that degrades fidelity to fit kbit/s;
  - idempotent state-resend loss repair;
  - multi-drone fusion.

  MinBand should emit ST 0903 and CoT at the ground side for compatibility, not compete with them.
- **Palantir and Anduril are where the tracks end up.** Their entity models are the natural sink for MinBand tracks in NATO contexts (MSS NATO, Lattice Entity API). MinBand is a feeder, not a COP.

### Gaps
- Not found:
  - any vendor's published bitrate for metadata-only or track-only modes (Helsing, QS, Tekever, Shield AI, Anduril);
  - whether Vector, AR3 or HX-2 can send detections only when video is denied;
  - "Avengers AI" running onboard drones (all evidence is central or cloud);
  - Shield AI EdgeOS link-loss and resync behaviour;
  - "The Swarm" as a distinct company;
  - "PALYCH" (Rise Technologies);
  - Kropyva's data rates.
- Not verified because the pages could not be opened: Helsing Altra and Auterion product pages (content came from search extracts).
- No independent test data on Nemyx, Altra, Underdog or TFL-1. All performance claims are company or trade press.

---

## 2. Research programmes and calls (2025–2026) on low-bandwidth ISR, semantic comms, and EW-resilient links

### Takeaway
DARPA's "Semantically-Aware ISR" SBIR (DPA26BZ05-DV019) is almost exactly MinBand's problem statement. It closed on 23 September 2026, is ITAR-restricted and is a US SBIR (US small business only). It is best used as validation and messaging, not as a funding route for a European or Ukrainian team. On the European side, the most recent relevant windows have closed:
- NATO DIANA's 2026 "Advanced Communication Technologies" and "Contested Electromagnetic Environments" challenges, and its 2027 call (closed 3 July 2026);
- UKDI's telemetry challenge (closed 12 May 2026);
- the EDF 2026 call (deadlines around 29 September 2026).

The live near-term options are:
- the EUDIS autumn hackathon (15–17 October 2026, theme "Autonomy on the Battlefield");
- the Brave1 rolling grants and Brave International joint-team calls;
- the next DIANA and UKDI cycles.

### Cited Findings

**DARPA Semantically-Aware ISR (DPA26BZ05-DV019).** I could not open darpa.mil (egress blocked); details below are from search extracts of the DARPA pages plus secondary coverage.
- **Problem framing:** payload sensor data far exceeds what tactical radio links can carry, "especially when jammed or squeezed to a few kilobits per second". Target platforms are Group 1 (RQ-11B Raven, RQ-20 Puma) and Group 2 (ScanEagle-class). [DARPA program page](https://www.darpa.mil/research/programs/semantically-aware-isr); [DARPA topic page](https://www.darpa.mil/work-with-us/opportunities/dpa26bz05-dv019)
- **What DARPA wants:** a "mission-aware semantic communications capability" that runs onboard, in real time, at low power. It should send "compact semantic packets" with the regions, events and context an operator needs. Approaches relying only on conventional compression, "fixed-category object detection", or cloud reachback are explicitly not of interest. [DARPA program page](https://www.darpa.mil/research/programs/semantically-aware-isr)
- **Metrics:** Phase II threshold is a 90% reduction against full-frame video baselines, with a 95–99% objective. Real-time embedded execution, with a path to about 2 W of added compute on Group 1/2 UAS. [DARPA program page](https://www.darpa.mil/research/programs/semantically-aware-isr); [Inside Defense: "slash tactical ISR data by up to 99%"](https://insidedefense.com/ai-news/darpa-seeks-ai-slash-tactical-isr-data-99)
- **Deliverable form:** "a small hardware-software module, an embedded SDK, or a sensor-pipeline plugin" integrable with ISR payloads, GCS and small satellites. [DARPA topic page](https://www.darpa.mil/work-with-us/opportunities/dpa26bz05-dv019)
- **Phase II goals** (per the BW&CO guide): turning natural-language operator requests into mission logic without mid-flight retraining, and reasoning across frames rather than fixed categories. Bidders are expected to have shown multimodal (EO/IR/event-camera) fusion below 5 W. [BW&CO](https://www.bwcoconsulting.com/fod/darpa-sbir-dv019-semantically-aware-isr)
- **Dates:** published 5 August 2026, opened 26 August 2026, closed 23 September 2026 at 12:00 ET. [DARPA topic page](https://www.darpa.mil/work-with-us/opportunities/dpa26bz05-dv019); [DoD SBIR 2026 P1 instructions](https://www.dodsbirsttr.mil/submissions/api/public/download/solicitationDocuments?solicitation=DOD_SBIR_2026_P1_CBZ&amp=&documentType=INSTRUCTIONS&amp=&component=DARPA&amp=&release=5)
- **Funding:** $1.5m over 18 months plus a $0.5m 6-month option. Described as Direct-to-Phase II, with no Phase I award. One third-party site lists $2m, and one SBIR portal lists it under Phase I. Conflict noted; the DARPA text is authoritative. [BW&CO](https://www.bwcoconsulting.com/fod/darpa-sbir-dv019-semantically-aware-isr); [SBIR portal](https://sbir.porbanderwala.cloud/opportunities/f862eed2-25a9-4c34-91ea-7d0f5eeb8e2b)
- **Restrictions:** ITAR-restricted (one summary says ITAR and EAR). Projected CMMC Level 2 (Self). [DARPA topic page](https://www.darpa.mil/work-with-us/opportunities/dpa26bz05-dv019)

**NATO DIANA**
- **2026 challenge programme:** ten challenges, two directly relevant.
  - "Advanced Communication Technologies": resilience for fast data exchange across decentralised computing, sensing and comms networks.
  - "Contested Electromagnetic Environments": "dual-use sensing, navigation, data link, spectrum analysis … in electromagnetic contested and congested environments".
  - [6GWorld](https://6gworld.com/natos-2026-diana-challenge-programme/); [techUK](https://www.techuk.org/resource/nato-s-2026-diana-innovation-challenges-announced.html); [DIANA 2026 CfP PDF](https://www.diana.nato.int/resources/site1/general/challenges/docs/2026-challenge-programme-cfp.pdf)
- **2026 cohort:** 150 companies from 24 nations announced 10 December 2025, out of about 3,600–3,680 applications. [EDR Magazine](https://www.edrmagazine.eu/nato-defence-innovation-accelerator-announces-largest-ever-cohort-of-150-innovators-to-work-on-ten-defence-and-security-challenges-in-2026); [Neuron](https://neuron.world/news/nato-diana-2026)
- **2027 call:** six areas, including "Operational Resilience in Contested Environments" (adversaries disrupting sensing, comms and PNT; seeks "practical, interoperable sensing, communication and PNT solutions") and "Multidomain Sensing and Advanced Data Processing for Intelligence and Surveillance". Closed 3 July 2026; the cohort starts January 2027. [The Quantum Insider](https://thequantuminsider.com/2026/06/02/nato-diana-announces-six-new-challenges-to-tackle-evolving-defense-and-security-needs/); [DIANA 2027 challenge PDF](https://www.diana.nato.int/resources/site1/general/challenges/challenges2027/challenge_pdf/2027_operational_resilience_in_contested_environments.pdf); [Tehnopol](https://www.tehnopol.ee/en/nato-diana-opens-new-call-for-applications-across-six-defence-and-security-challenge-areas/)
- **Funding precedent:** DIANA sites such as DualTech by Takeoff in Turin (run by Plug and Play) gave selected startups €100k plus a six-month bootcamp (2025 example). [DIANA DualTech](https://www.diana.nato.int/accelerator-programme/takeoff.html); [EconomyUp](https://economyup.it/innovazione/la-nato-sceglie-lacceleratore-di-plug-and-play-a-torino-per-trovare-startup-ecco-le-prime-6)

**EU: EDF, EUDIS, EDIP**
- **EDF 2026:** about €1bn across 10 calls and about 28–31 topics. Record 612 proposals. Deadlines were generally around 29 September 2026, so now closed.
  - Relevant topics include a swarm-based tactical-awareness challenge (EDF-2026-LS-RA-CHALLENGE-DIGIT-…) and digital-transformation topics on "AI-supported situational awareness and collaborative systems based on unmanned platforms".
  - [EDF WP 2026](https://defence-industry-space.ec.europa.eu/document/download/3991ef09-1f18-44d1-baf5-dbe60bbb928a_en?filename=EDF+Work+Programme+2026.pdf); [EDF 2026 call topic descriptions](https://defence-industry-space.ec.europa.eu/document/download/2cd25753-d14f-4019-8fdf-68cca55b7f17_en?filename=EDF+2026+Call+Topic+Descriptions.pdf); [Defense Watch: 612 proposals](https://thedefensewatch.com/policy-strategy/european-defence-fund-612-proposals-2026/); [Zabala](https://www.zabala.eu/news/european-defence-fund-2026/)
- **EDF 2025 results** (announced April 2026): €1.07bn for 57 projects. [Zabala](https://www.zabala.eu/news/european-defence-fund-2026/)
- **EUDIS Defence Hackathon (5th edition):** 15–17 October 2026, on-site in Cyprus, Austria, Ireland, Italy, Latvia, Norway, Portugal and Sweden. Theme: "Autonomy on the Battlefield".
  - The spring 2026 edition (Kraków, 26–28 March, airspace defence) had a €10k prize pool and 40 h of mentoring for winners.
  - [CBN Cyprus](https://www.cbn.com.cy/article/133039); [AGH Kraków](https://www.agh.edu.pl/en/calendar/detail/s/eudis-defence-hackathon-and-mentoring-spring-2026)
- **EUDIS Business Accelerator:** cohort 3 is 20 companies from a record 499 applications, starting September 2026; the cohort 4 window closed 30 May. 8-month programme.
  - EUDIS Business Coaching (up to 15 expert days over 6 months) is only for SMEs already in EDF projects.
  - The Defence Equity Facility is €175m, investing via funds.
  - [EC notice](https://defence-industry-space.ec.europa.eu/new-eudis-defence-business-accelerator-call-2026-04-16_en); [Starburst](https://starburst.aero/news/unveiling-of-the-20-companies-selected-for-the-third-cohort-of-the-eudis-business-accelerator/); [Council doc WK-364-2026](https://data.consilium.europa.eu/doc/document/WK-364-2026-INIT/en/pdf); [EUDIS](https://eudis.europa.eu/index_fr)
- **EDIP 2026–27:** described as about €1.5bn with drone and AI emphasis (secondary coverage; separate from EDF). [RobotToday](https://robottoday.com/article/eu-bets-1-5-b-on-drones-and-ai-inside-edip-s-2026-2027-defence-industry-programme)

**Ukraine: Brave1 calls**
- **General grants:** relaunched July 2026 with UAH 500k–8m per project depending on TRL (about $12k–$192k). 53 technology priorities across 9 areas; full list at grants.brave1.tech.
  - The separate BraveTech EU programme gives up to UAH 8m regardless of TRL, with a functional prototype as the outcome.
  - [MoD Ukraine](https://mod.gov.ua/en/news/brave1-launches-new-grants-for-defense-technology-projects-to-strengthen-the-defence-forces-of-ukraine-s-technological-advantage-on-the-battlefield); [Militarnyi](https://militarnyi.com/en/news/from-uah-500-thousand-to-8-million-brave1-has-resumed-its-grant-program/); [Euromaidan Press](https://euromaidanpress.com/2026/07/13/ukraines-new-grant-list-reads-like-map-of-war-exoskeletons-dugout-busters-lasers-and-humanoids/)
- **Scale:** more than 1,000 grants awarded (September 2026). EW/SIGINT/ELINT was among the most-funded categories (49 grants). Stated areas include "communication and power supply technologies". [MoD Ukraine](https://mod.gov.ua/en/news/brave1-has-awarded-more-than-1-000-grants-to-ukrainian-defense-developers); [GlobalSecurity mirror](https://www.globalsecurity.org/wmd/library/news/ukraine/2026/09/ukraine-260914-ukraine-mod02.htm)
- **Brave International:** the Cabinet framework opening participation to foreign developers. It covers UNITE-Brave NATO, Brave Norway, Brave France, Brave Germany and Brave Lithuania, funded by equal Ukraine and partner contributions. All developments must pass Test in Ukraine.
  - UNITE-Brave NATO requires joint teams of one Ukrainian company and one NATO-country company, with the Ukrainian partner submitting.
  - Its first call was C-UAS/air defence, targeting TRL 8; the EoI deadline was 3 August (2026).
  - A separate earlier €10m NATO–Brave1 competition covered SIGINT, electronic attack on drones, high-altitude platforms and autonomous guidance.
  - [Brave1 UNITE-Brave NATO](https://brave1.gov.ua/en/news/unite-brave-nato-open-call-eng); [Brave1 Brave International](https://brave1.gov.ua/en/news/brave-international-eng); [MoD Ukraine](https://mod.gov.ua/en/news/over-100-million-for-defense-technologies-government-gives-the-green-light-to-international-grants-from-brave1-and-partner-countries); [Kyiv Post](https://www.kyivpost.com/post/72587)
- **Battle Proven 2026:** startup competition at Defense Tech Valley, Lviv, 16–17 September 2026. Selection partners were the Unmanned Systems Forces, the 3rd Army Corps and the 2nd NGU Corps "Khartiia". Open to foreign startups. [GlobalSecurity mirror of MoD](https://www.globalsecurity.org/wmd/library/news/ukraine/2026/05/ukraine-260526-ukraine-mod03.htm)

**UK (UKDI, formerly DASA)**
- DASA is now part of UK Defence Innovation (UKDI). [UKDI news](https://www.gov.uk/government/news/ukdi-launches-new-phase-of-fast-past-innovation-competition)
- **Innovation Support to Operations Phase 3, Cycle 7:** seven challenges, including a telemetry challenge seeking "low-probability-of-detection, jam-resistant" telemetry over at least 300 km, plus GNSS-free navigation and UAS survivability. Up to £350k per proposal; TRL 6 within 6 months. Closed 12 May 2026; projects start September 2026. [IUK Business Connect](https://iuk-business-connect.org.uk/opportunities/uk-defence-innovation-competition-innovation-support-to-operations-phase-3-cycle-7/); [DSEI](https://www.dsei.co.uk/news/uk-seeks-emerging-tech-solutions-under-multi-cycle-competition)
- **Earlier UK competition:** "Affordable and adaptable UAS autonomy". [GOV.UK](https://www.gov.uk/government/publications/competition-affordable-and-adaptable-unmanned-air-systems-autonomy/affordable-and-adaptable-unmanned-air-systems-autonomy)

**Germany**
- **BwPBBG** (Bundeswehr procurement acceleration law): in force since 14 February 2026. [Keen VP](https://www.keenventurepartners.com/posts/europes-biggest-defence-budget-also-one-of-the-hardest-to-reach-keen-venture-partners-publishes-the-founders-guide-to-german-defence-procurement)
- **CIHBw (Cyber Innovation Hub):**
  - 2026 budget raised to €40m under a scaling mandate. Reported totals: 217 projects launched, 80 MVPs tested, 52 still in service. [Keen VP](https://www.keenventurepartners.com/posts/europes-biggest-defence-budget-also-one-of-the-hardest-to-reach-keen-venture-partners-publishes-the-founders-guide-to-german-defence-procurement); [Table.Briefings](https://table.media/en/security/news/bundeswehr-cyber-innovation-hub-the-german-armed-forces-plans-for-its-increased-budget)
  - It can get something to soldiers "within 180 days", but larger buys revert to regular procurement. [Xpert.Digital](https://xpert.digital/en/bundeswehr-cyber-%E2%80%8B%E2%80%8Binnovation-hub/)
- **InnoZBw** (Erding): opened February 2026 with more than 330 staff covering AI and drones. [Xpert.Digital](https://xpert.digital/en/innovation-center-of-the-german-armed-forces/)
- **Innovationspartnerschaft:** the Bundeswehr funds development and then buys, and the startup keeps the IP. [Keen VP](https://www.keenventurepartners.com/posts/europes-biggest-defence-budget-also-one-of-the-hardest-to-reach-keen-venture-partners-publishes-the-founders-guide-to-german-defence-procurement)

**France**
- **DGA ELISA** innovation partnership (10 April 2026): autonomous AI interceptor drones, about €18.7m, deadline 7 May 2026. [ActuIA](https://www.actuia.com/acteur/dga/)
- **AID EPERVIER:** anti-FPV call. [Démarches simplifiées](https://www.demarches-simplifiees.fr/commencer/appel-a-projets-epervier/dossier_vide)
- **AID 2025:** committed €1.173bn. [AID bilan 2025](https://www.defense.gouv.fr/aid/actualites/lagence-linnovation-defense-presente-son-bilan-dactivites-2025)
- **ASTRID-AI** research call: announced via ANR. [ANR](https://anr.fr/en/latest-news/read/news/recherche-et-innovation-defense-un-futur-appel-a-projets-astrid-sur-lintelligence-artificielle/)

### Inferences
- **DARPA as validation, not funding.**
  - MinBand's measured 60–800 B/s against 1.5 Mbit/s is a reduction of roughly 99.6–99.97%, beyond DARPA's 95–99% objective. Caveat: DARPA's baseline is "full-frame video" and its goal is preserving mission context, so the comparison is only indicative.
  - DARPA explicitly says "fixed-category object detection" alone is not of interest. MinBand should frame itself as mission-aware entity state with a prediction model and fidelity budgets, and ideally add open-vocabulary or operator-tasked semantics (events, regions of interest).
  - This topic is useful validation for European pitches ("DARPA has defined this as a problem"). It is probably not accessible: SBIR generally requires a US-owned small business (general SBIR rule, not verified for this topic), and the topic is ITAR-restricted.
  - Building MinBand inside an ITAR-restricted US programme could later conflict with an ITAR-free European positioning (see Q3).
- **Best-fit European windows are cyclical.** These are DIANA (Contested EM / Operational Resilience), UKDI ISO cycles (jam-resistant telemetry) and the EDF digital-transformation topics. The next openings are likely mid-2027. The immediate action items are:
  - the EUDIS autumn hackathon (theme fits "autonomy");
  - Brave1 grants via a Ukrainian partner or entity;
  - the CIHBw 180-day track in Germany.
- **EDF is a consortium play.** MinBand would join a consortium as an SME subcontractor or partner, not lead. EDF generally requires multinational consortia (from general knowledge; verify). Once inside an EDF project, MinBand qualifies for EUDIS Business Coaching.

### Gaps
- Could not read DARPA solicitation text directly. Topic-specific eligibility (foreign ownership), the exact phase structure and the TPOC are unconfirmed.
- No 2026 German CODE (UniBw research institute) or BAAINBw-specific call on low-bandwidth ISR found.
- No dedicated Brave1 challenge on semantic or low-bandwidth ISR found. The 53-priority list was not retrievable; it is at grants.brave1.tech.
- No French AID call specifically on data links or semantic comms found.
- The EDIP 2026–27 topic list was not verified from a primary source.

---

## 3. Pathways to field: Ukraine, NATO/EU, accelerators, investors, Bundeswehr; timelines and paperwork

### Takeaway
Ukraine is the fastest route to real users. The sequence is:
1. Brave1 (grant plus Test in Ukraine) and a unit partner (via Battle Proven or direct relationships).
2. MoD codification, much simplified in March 2026, or the "innovative products for combat testing" pilot.
3. A listing on Brave1 Market / DOT-Chain Defence, where units spend e-points and DOT-Chain prepays up to 70%.

Foreign teams generally need a Ukrainian partner or entity. Exports out of Ukraine are now possible but tightly controlled (since July 2026). In parallel, Germany's CIHBw (180-day prototype track) and the NATO DIANA and EUDIS accelerators give Western validation. VC appetite is at a record: about $7.4bn in European defence-tech VC year-to-date in 2026.

### Cited Findings

**Ukraine: Brave1, Test in Ukraine, Market, DOT-Chain, codification**
- **Brave1 Market scale:** more than 1,100 orderable items and more than 400 combat units (August 2026). More than 590,000 UAVs ordered in the first nine months of 2026. [GlobalSecurity mirror of MoD](https://www.globalsecurity.org/wmd/library/news/ukraine/2026/09/ukraine-260925-ukraine-mod02.htm); [DroneXL](https://dronexl.co/2026/04/05/ukraine-military-drone-marketplace-brave1/)
- **Buying mechanics:**
  - Units spend combat-earned e-points. Orders flow through DOT-Chain Defence to the manufacturer, which gets "prepayments of up to 70 per cent" on order acceptance.
  - Product cards carry specs and reviews, and makers see usage statistics.
  - [Censor.net on DOT-Chain](https://censor.net/en/resonance/3579692/dot-chain-defence-how-it-works-how-manufacturers-and-military-feel-about-it); [NV](https://english.nv.ua/nation/drone-army-bonus-military-personnel-select-equipment-based-on-combat-points-through-brave1-market-50555904.html); [Ukraine's Arms Monitor](https://ukrainesarmsmonitor.substack.com/p/brave1-market-ukraines-catalogue)
- **Codified vs non-codified:** sources conflict. Some say only certified or codified systems are orderable; others say Brave1 Market lets units buy non-codified products, unlike the Defence Procurement Agency. [Defender Media](https://thedefender.media/en/2025/07/ebaly-exchange-for-drones/); [Ukraine's Arms Monitor](https://ukrainesarmsmonitor.substack.com/p/brave1-market-ukraines-catalogue)
- **Custom orders (August 2026):** units post TTX requirements, suppliers bid blind, and a reverse auction picks the lowest price among compliant bids. Currently limited to FPV, fibre FPV and fixed-wing interceptors. [Defender Media](https://thedefender.media/en/2026/08/brave1-market-custom-drones/); [Brave1 on X](https://x.com/BRAVE1ua/status/2090738376961863978)
- **Test in Ukraine:**
  - Established July 2025. Lets international firms test in frontline conditions at equipped ranges and get unit feedback. Targets UAV, EW and AI products.
  - Airbus signed the first Western "Brave Prime" strategic partnership (around 30 June / 1 July 2026) for frontline co-testing; Saab has also joined.
  - [Mezha](https://mezha.net/eng/bukvy/brave1-launches-test-in-ukraine-platform-for-military-tech-testing/); [Airbus](https://www.airbus.com/en/newsroom/press-releases/2026-07-airbus-and-brave1-partner-to-boost-ukrainian-defence-innovation); [Kyiv Independent](https://kyivindependent.com/ukraines-brave1-signs-first-major-western-defense-industry-partnership-with-airbus/); [MoD Ukraine: Brave1 Advantage](https://mod.gov.ua/en/news/one-billion-for-explosives-low-cost-missiles-and-agreements-with-airbus-and-saab-key-takeaways-from-brave1-advantage)
- **Codification history:**
  - Admission to operation was cut to 20 days in November 2023 and codification to 10 days in May 2024.
  - On 2 March 2026 the Cabinet reform:
    - removed the requirement for prior state confirmation of urgent need;
    - let manufacturers approve their own technical specifications;
    - made a manufacturer's quality certificate sufficient for procurement.
  - The MoD headline says "only five documents" are needed.
  - [MoD Ukraine](https://mod.gov.ua/en/news/only-five-documents-the-ministry-of-defence-made-the-weapons-codification-procedure-as-simple-as-possible-for-manufacturers); [Sayenko Kharenko legal digest Mar–Apr 2026](https://sk.ua/legal-digest-developments-in-ukraine-s-defence-sector-march-april-2026/); [Razom](https://razomua.media/en/news/biznes/kabmin-skorotyv-shlyakh-vid-prototypu-do-frontu-shcho-zminyuye-sproshchena-kodyfikatsiya-ozbroyennya)
- **Codification vs adoption:** the MoD distinguishes codification (entry in the supply catalogue) from adoption into service. [MoD Ukraine](https://mod.gov.ua/en/news/adoption-into-service-authorization-for-operational-use-and-codification-of-new-weapons-and-military-equipment-the-ministry-of-defence-clarifies-concepts-and-differences)
- **Combat-testing pilot:** the MoD may buy "a new or improved sample … with no analogues in service" under a simplified procedure and transfer it to designated units for experimental combat use. [Sayenko Kharenko](https://sk.ua/ukraina-sproshhuie-kodifikaciju-vijskovih-virobiv-ta-prishvidshuie-kontraktuvannja-novih-rozrobok/)
- **Military testing volume:** the General Staff logged 433 tests of new samples in H1 2026, and the "Iron Polygon" programme ran about 150 experimental studies (113 on unmanned systems). [Mezha](https://mezha.net/eng/?p=3112247)
- **Ukrainian accelerators:** Defence Builder Accelerator (with Genesis/Sigma and KSE) is a four-month programme requiring an MVP plus military feedback, ending in a demo day with about 70 investors (launched 2024; 2026 status unconfirmed). [Tech.eu](https://tech.eu/2024/04/17/from-garage-to-global-new-program-fast-tracks-ukrainian-defence-startups/); [Mezha](https://mezha.net/eng/bukvy/57fe5dac_nine_ukrainian_defence/)

**Ukraine export controls (if MinBand is built or co-built in Ukraine)**
- **Timeline:** controlled exports started around February 2026. On 1 July 2026 the Cabinet adopted a procedure for controlled export of military and dual-use goods and technologies during martial law. [Avellum](https://avellum.com/?p=15154); [Lawfare](https://www.lawfaremedia.org/article/the-red-tape-of-ukraine-s-semi-open-arms-exports)
- **Who can buy:** only "Drone Deal" partner states (reported: Netherlands, Lithuania, Latvia).
- **What is covered:** products must be codified or adopted. Thresholds apply: UAH 15m or more for finished products and technologies, none for components.
- **Conditions:** IP stays registered in Ukraine and is transferred "for use only". Re-export needs the State Export Control Service's permission. Permits can be suspended if the Ukrainian forces need the item.
- [Avellum](https://avellum.com/?p=15154); [Euronews](https://euronews.com/my-europe/2026/04/28/ukraine-says-it-will-open-arms-exports-with-drone-deals-but-not-to-all-countries); [Baker McKenzie](https://www.bakermckenzie.com/en/insight/publications/alerts/2025/11/ukraine-controlled-weapons-export-procedure)

**EU and US export controls, ITAR-free positioning, SAFE**
- **EU dual-use:** Regulation (EU) 2021/821 was updated in September 2025, expanding controls on advanced chips, quantum and AI systems. Advice from one analysis: classify your stack against Annex I before the first VC cheque, and separate controlled from uncontrolled modules. One analysis also notes a fragmented national dual-use licensing regime that can block intra-NATO sales. [Algeria Tech summary](https://algeriatech.news/?p=25971); [Militär Aktuell: "The ITAR effect"](https://militaeraktuell.at/en/the-itar-effect-the-underestimated-risk-to-europes-drone-programs/)
- **SAFE:** the EU programme requires at least 65% European-origin components in EU-funded projects. [Sacra](https://sacra.com/chat/h/bc9810f2-3af4-4e58-85b3-40f4c6f1ecf1/) (generated-analysis source; verify against the SAFE regulation)
- **ITAR-free positioning:** "Even if you have US engineers, you become Itar-tainted." Helsing and Auterion both market ITAR-free products. [Times report reproduced](https://scribe.disroot.org/post/2523894)
- **Cautionary example:** Origin Robotics' BLAZE framework is SAFE-financed. [Janes](https://www.janes.com/defence-intelligence-insights/defence-news/air/latvia-orders-more-blaze-interceptor-uavs-under-framework-agreement-with-origin-robotics)

**NATO DIANA and EUDIS**
- See Q2 for DIANA cohort sizes (150 companies from about 3,680 applications) and EUDIS details (8-month accelerator for 20 companies; hackathon mentoring). [EDR Magazine](https://www.edrmagazine.eu/nato-defence-innovation-accelerator-announces-largest-ever-cohort-of-150-innovators-to-work-on-ten-defence-and-security-challenges-in-2026); [Munich Startup](https://www.munich-startup.de/en/120961/defensetech-eudis-business-accelerator-2026/)

**Investors active in European defence (2026)**
- **Market size:** European defence-tech VC reached $7.4bn year-to-date in 2026 (Dealroom / Resilience Media), nearly triple 2025's $2.6bn, with about $10.5bn projected for the full year. Helsing's round is more than 85% of it. US investors provide about 47%. [Tech.eu, 5 Oct 2026](https://tech.eu/2026/10/05/european-defencetech-hits-record-7-4b-as-investment-nearly-triples-in-2026/); [Trending Topics](https://www.trendingtopics.eu/european-defense-tech-funding-record-2026/)
- **Most active investors:** NATO Innovation Fund (9) and Project A (8) since 2024. [Tech.eu](https://tech.eu/2026/10/05/european-defencetech-hits-record-7-4b-as-investment-nearly-triples-in-2026/)
- **Lakestar:** closed a $300m defence fund (Resilience I) in September 2026. [Resilience Media](https://resiliencemedia.co/lakestar-closes-300m-defence-fund-and-warns-europe-off-us-tech-reliance); [Lakestar](https://www.lakestar.com/resiliencepress)
- **Expeditions:** Warsaw/London firm, closed a €197m Fund II in July 2026 with BAE backing. At least half is for follow-ons, tickets up to €20m. Portfolio includes UForce, Orqa, Frankenburg and ComandAI. [Resilience Media](https://resiliencemedia.co/expeditions-closes-its-latest-fund-at-e197m-smashing-through-its-original-target/); [TNW](https://thenextweb.com/news/expeditions-197m-defence-fund-bae-nato)
- **General Catalyst and Plural:** both participated in Helsing's Series E. [Helsing](https://helsing.ai/newsroom/helsing-raises-1-8bn-in-series-e)
- **Founders Fund and Sequoia:** in Stark. [DroneXL](https://dronexl.co/2026/06/24/thiel-sequoia-570m-german-drones/)
- **Ukrainian-linked funds:** D3, UA1 VC and Green Flag (in Swarmer's round); Double Tap (Griselda); Varangians (NORDA). [DroneXL](https://dronexl.co/2025/10/13/ukrainian-drone-swarm-startup-swarmer)

### Inferences
**Suggested sequence for MinBand.** This is inference from the mechanics above, not a sourced plan.

| Step | Timing | Actions |
|---|---|---|
| 0 | Now to November 2026 | EUDIS autumn hackathon (15–17 October, "Autonomy on the Battlefield"). Demo against a real narrowband radio, not just synthetic evals. |
| 1 | 0–3 months | Find a Ukrainian partner, either a drone OEM (e.g., someone like Frontline, SkyFall or Vyriy) or a software house. Apply jointly to a Brave1 grant (UAH 0.5–8m by TRL) or the next Brave International call. Book Test in Ukraine slots. |
| 2 | 3–9 months | Get unit feedback via Battle Proven-style competitions or direct unit relationships (USF, 3rd Army Corps, Khartiia were the 2026 selection partners). The deliverable units will judge is: does it show up in DELTA / Kropyva / ATAK? |
| 3 | 6–12 months | Codify (simplified since March 2026, manufacturer-written specs) or enter the "combat testing" pilot. List on Brave1 Market so units can buy with e-points. A software-only SDK may have to be listed as part of an OEM's drone or as a module, since Market categories are hardware-centric. |
| 4 | In parallel | CIHBw 180-day track or an Innovationspartnerschaft in Germany. Next DIANA call (Contested EM / Operational Resilience). Next UKDI ISO cycle (jam-resistant telemetry). |

- **Entity and IP structure decided early:**
  - An EU (non-US) entity keeps the ITAR-free claim.
  - Ukrainian co-development means IP registered in Ukraine falls under the July 2026 export rules (use-only licences, re-export permits).
  - Keep the core protocol and predictor IP in an EU entity and license it to a Ukrainian partner. This is a common pattern but needs legal advice.
- **Avoid DARPA SAI or other US-ITAR work on the core codebase** unless the US market is the priority. Even US engineers can "taint" the product.
- **Dual-use classification:** a protocol or compression SDK without targeting functions is likely easier to classify as dual-use than as munitions. Classify against Annex I early, especially the crypto and encryption features of the link layer.

### Gaps
- Seller onboarding requirements for Brave1 Market (forms, whether software-only SDKs are listable, eligibility of foreign or non-codified suppliers) were not found. Sources conflict on non-codified listings.
- Whether foreign companies can get Brave1 domestic grants without a Ukrainian entity: evidence suggests not (domestic grants list Ukrainian-registered applicants), but this is unconfirmed.
- No sourced detail on how units like Magyar's Birds (now the Unmanned Systems Forces leadership), 3rd Assault Brigade or Aerorozvidka onboard startups, beyond Battle Proven selection partners and Aerorozvidka's role in UA DroneID ([Pravda](https://www.pravda.com.ua/eng/news/2024/04/10/7450583/)).
- No information found on Join Capital's defence activity, the 2026 Plug and Play defence cohorts, or Defence Builder's 2026 status.
- The current codification day count after the March 2026 reform is not stated in the sources.
- EU dual-use classification of a "semantic ISR compression SDK" is not addressed in any source; legal advice needed.

---

## 4. What buyers and programmes say they want (2025–2026)

### Takeaway
Stated demand clusters around:
- EW and GNSS resilience;
- cheap mass;
- operator-to-drone ratio (one operator, many drones);
- interoperability (SitaWare, TAK/CoT, DELTA, NATO standards);
- software-defined upgrades (Nemyx "via software update");
- sovereignty (ITAR-free, non-Chinese parts, SAFE 65% EU content).

Buyers also explicitly want *more* bandwidth for HD video over mesh. That is a market-education challenge for MinBand: video stays the default, and MinBand must sell itself as what keeps the picture alive when video fails, and as the thing that lets one link serve many drones.

### Cited Findings
- **Bandwidth for video:** the Ukrainian MoD adviser wants mesh modems for UAV control at 300–400 km "with enough bandwidth for stable HD video", built domestically because Western modems are too expensive. [Euromaidan Press](https://euromaidanpress.com/2026/04/29/ukraine-needs-to-build-its-own-high-speed-mesh-tech-as-china-is-russias-supplier-and-western-modems-are-too-expensive/)
- **Operator-to-drone ratio:**
  - Swarmer: one operator, many drones. [DroneXL](https://dronexl.co/2025/10/13/ukrainian-drone-swarm-startup-swarmer)
  - Helsing: "less operators and more drones". [Interesting Engineering](https://interestingengineering.com/innovation/uk-new-hx-2-strike-drone)
  - Lasar's Group: "one pilot, five bombers" (August 2026). [Euromaidan Press](https://euromaidanpress.com/2026/08/22/one-pilot-five-bombers-ukraines-lasars-group-ties-whole-raid-to-single-operator-to-save-manpower/)
- **Software-defined upgrades:** Auterion says any compatible drone can join a Nemyx swarm through a software update, and Shrike drones will gain swarming "with no airframe changes or extra hardware". [Auterion](https://auterion.com/product/nemyx/); [Auterion/SkyFall](https://auterion.com/auterion-and-skyfall-to-ship-50000-shrike-strike-drones-to-ukraines-front-lines/)
- **Interoperability:**
  - Helsing integrated Altra with SitaWare. [Janes](https://www.janes.com/defence-intelligence-insights/defence-news/security/dsei-2025-helsing-and-systematic-partner-on-swarming-recce-strike-c2-system)
  - Auterion offers TAK/CoT both ways. [Auterion](https://auterion.com/product/skynode-s/)
  - DELTA tested at CWIX and integrated with TOPAZ. [NATO ACT](https://www.act.nato.int/article/delta-system-cwix/)
  - DIANA 2027 seeks "interoperable" sensing, comms and PNT. [The Quantum Insider](https://thequantuminsider.com/2026/06/02/nato-diana-announces-six-new-challenges-to-tackle-evolving-defense-and-security-needs/)
- **Sovereignty, cost and EU content:**
  - SAFE requires 65% European-origin components (secondary source). [Sacra](https://sacra.com/chat/h/bc9810f2-3af4-4e58-85b3-40f4c6f1ecf1/)
  - ITAR-free is a marketing differentiator. [Times via repost](https://scribe.disroot.org/post/2523894)
  - Ukrainian makers are localising away from Chinese parts at price parity. [Euromaidan Press](https://euromaidanpress.com/2024/12/20/ukrainian-company-assembles-first-fully-domestic-fpv-drone/); [Mind.ua](https://mind.ua/news/20300885-skyfall-pogliblyue-lokalizaciyu-droniv-vampire-i-shrike)
- **Price discipline:** Brave1 Market custom orders use automated reverse auctions where "the lowest price wins". [Defender Media](https://thedefender.media/en/2026/08/brave1-market-custom-drones/)
- **Rapid fielding over perfection:**
  - UKDI ISO cycle requires TRL 6 within 6 months. [IUK](https://iuk-business-connect.org.uk/opportunities/uk-defence-innovation-competition-innovation-support-to-operations-phase-3-cycle-7/)
  - CIHBw promises 180 days. [Xpert.Digital](https://xpert.digital/en/bundeswehr-cyber-%E2%80%8B%E2%80%8Binnovation-hub/)
  - UNITE-Brave NATO targets TRL 8 with mandatory Test in Ukraine. [Unmanned Airspace](https://www.unmannedairspace.info/counter-uas-systems-and-policies/ukraines-brave-1-and-nato-open-unite-brave-nato-procurement-programme-prioritising-c-uas/)
- **Risk of under-delivering:** Stark's Virtus reportedly missed in four of four attempts in October 2025 trials, and German lawmakers capped its contract. Buyers punish demo failures. [DroneXL](https://dronexl.co/2026/01/27/helsing-stark-rheinmetall-loitering-munition); [DroneXL](https://dronexl.co/2025/11/03/germany-awards-950m-drone-contract)
- **Data and AI demand:**
  - Ukraine offers Avengers Labs data (5 million frames) to industry for model training. The UK wants it for "decision support and target identification". [Janes](https://www.janes.com/defence-intelligence-insights/defence-news/security/uk-considering-using-ukraines-avengers-labs-data-for-target-identification)
  - DARPA wants mission-aware context, not fixed-class detection. [DARPA](https://www.darpa.mil/research/programs/semantically-aware-isr)

### Inferences
- **What the pitch should contain:**
  - a measured bytes-per-second and latency curve on a real jammed or narrowband radio (e.g., MAVLink serial telemetry radio, LoRa/Meshtastic, HF) next to a video baseline;
  - output into TAK/CoT and DELTA-compatible formats;
  - a software-only install path on Skynode / AuterionOS or Jetson-class companion computers;
  - an ITAR-free, EU-content story.
- **Lead with many drones per link.** "How many drones can one 9.6 kbit/s link support with a live picture?" maps directly onto the operator-to-drone ratio buyers care about.
- **The commercial unit is per-drone licence cost.** In a reverse-auction, e-points market where drones cost hundreds to low thousands of dollars, a software module must be priced at a small fraction of the airframe. This is inference; no price data found.

### Gaps
- No published cost-per-unit targets for ISR drones or software modules from Ukraine's MoD, the Bundeswehr or NATO were found.
- No explicit mandate that drones must integrate with DELTA was found. A search returned nothing confirming such a requirement.
- No buyer statement explicitly asking for "detections instead of video" was found, apart from DARPA's topic.

---

## 5. White space MinBand could own, and business-model angles

### Takeaway
The open niche is an OEM- and radio-agnostic semantic ISR link layer. It would be an embeddable SDK with:
- a shared-predictor protocol on both ends;
- byte-budget control;
- loss repair by state resend;
- multi-drone fusion;
- output into the C2 systems already in use (TAK/CoT, MISB ST 0903, DELTA, Lattice, Altra/SitaWare).

Precedents show OEMs will license autonomy software modules: Swarmer, NORDA, TFL-1, Sine, and Auterion's app model. But Swarmer's thin 2025 revenue ($310k, largely one OEM customer) is a warning about concentration risk in a pure software licensing model.

### Cited Findings
- **OEM licensing of software is established in Ukraine:**
  - Swarmer: "drone manufacturers also license Swarmer's software". [DroneXL](https://dronexl.co/2025/10/13/ukrainian-drone-swarm-startup-swarmer)
  - NORDA: integrated with "dozens of Ukrainian drone manufacturers" (claim). [DroneXL](https://dronexl.co/2025/09/25/ukraine-drone-norda-dynamics-gps-free-flight)
  - Sine: more than 50 drone makers (claim). [AOL/BI](https://www.aol.com/ukrainian-tech-company-working-beat-113702269.html)
  - TFL-1 module: more than 50 units (claim). [Kyiv Post](https://www.kyivpost.com/amp/post/70214)
- **Concentration risk:** most of Swarmer's 2025 revenue ($309,920) came from one OEM contract, and the loss of a key Ukrainian partner cut revenue roughly five-fold (single source). [dev.ua](https://dev.ua/en/news/pershyi-finansovyi-zvit-swarmer-pislia-ipo-1778840282)
- **App-store model on autopilots:** Nemyx is "an app on AuterionOS". [Auterion](https://auterion.com/product/nemyx/); [Auterion docs app framework](https://docs.auterion.com/app-development/app-framework/app-framework-1)
- **Gated developer model at the integration sink:** Anduril runs a gated developer programme with gRPC Entity APIs. [Anduril docs](https://docs.anduril.com/guide/overview)
- **The standards gap MinBand could fill:**
  - MISB ST 0903 allows standalone track streams but defines no prediction or budget logic. [Impleo](https://www.impleotv.com/content/misbcore/help/ST903/st903.html)
  - The Meshtastic TAKPacket shows compressed CoT at about 87 B median per message over LoRa. [TAKPacket SDK](https://klibs.io/project/meshtastic/TAKPacket-SDK)
  - Isode documents TAK over HF with XML CoT messages of "a few hundred bytes". [Isode](https://www.isode.com/wp-content/uploads/2026/05/Operating-TAK-over-HF-Radio.pdf)
- **Dual-use buyers:**
  - Delian's systems are used by the Greek army for sea-border monitoring. [newmoney.gr](https://www.newmoney.gr/roh/palmos-oikonomias/epixeiriseis/delian-alliance-industries-i-eteria-pou-echi-vali-tin-ellada-ston-evropaiko-charti-tis-amintikis-kenotomias/)
  - DIANA and EDF challenges explicitly require "dual-use" framing. [6GWorld](https://6gworld.com/natos-2026-diana-challenge-programme/)
  - Auterion positions Nemyx for "border surveillance" too. [Auterion](https://auterion.com/product/nemyx/)
- **Data partnerships:** Avengers Labs offers combat-annotated data to industry, which could train MinBand's onboard detector. [The Defense Post](https://thedefensepost.com/2026/08/12/ukraine-ai-drone-target-detection/amp/)

### Inferences
**Positioning statement.** "MinBand is the semantic link layer: it turns any drone's onboard detector into a live, dead-reckoned entity picture over links too thin or too jammed for video, and delivers it into TAK, DELTA, Lattice or SitaWare." It complements autonomy stacks (Auterion, Helsing, Swarmer, NORDA, TFL) and video links (Rise, Silvus/Doodle-class mesh); it competes with neither.

**Candidate white spaces**
1. **Vendor-neutral protocol and spec.** An open spec, possibly proposed as a MISB/NATO-friendly profile with an extension for shared predictor state, plus a commercial reference SDK. Open spec wins adoption; the SDK, fusion server and certification earn revenue.
2. **"Fits on the telemetry link".** A MAVLink-tunnelled entity stream that rides the serial low-bandwidth path. Auterion documents that this path carries no video. This turns every telemetry-only drone into an ISR source.
3. **Many-drones-per-link fusion.** Server-side multi-device fusion with a per-link budget allocator, where the commander's bandwidth is shared across a swarm. This aligns with the operator-to-drone ratio demand.
4. **Graceful degradation ladder.** Tracks always, thumbnails or chips when budget allows, full video on request. This matches DARPA's "regions, events and context" framing and keeps video-loving buyers comfortable.
5. **Relay and retrans economics.** Fewer bytes means fewer relay hops are needed and lower RF emission time (LPD/LPI benefit). UKDI's "low-probability-of-detection" telemetry ask is the hook. This benefit is inferred, not measured.

**Business models**
- **Per-unit runtime licence to OEMs**, priced as a small share of airframe cost, with volume tiers for Ukrainian mass production.
- **Ground or fusion server licence** per command post.
- **Integration and NRE contracts** with primes (Altra/SitaWare, Lattice via Rheinmetall, DELTA via MoD).
- **Grant-funded R&D** (Brave1, DIANA, EDF consortium role, CIHBw).
- **Dual-use editions:**
  - SAR, where many drones over a disaster zone share thin cellular or satellite links;
  - border and maritime monitoring, where SATCOM bytes are expensive (Delian, Tekever-type customers);
  - wildfire, firefighting and police over LTE dead zones.

**Main risks**
- Primes building it in-house: Anduril Lattice is "local-first" and DARPA is now funding US competitors.
- DELTA integration dependency in Ukraine.
- OEM concentration, as in the Swarmer example.
- ITAR or dual-use classification choices that close markets.
- Evaluation credibility: synthetic evals versus field radios, as the Stark trial failure illustrates.

### Gaps
- No market sizing for "semantic ISR link" software, and no per-unit price benchmarks for autonomy modules (NORDA, TFL, Swarmer licence prices not disclosed).
- No evidence whether DELTA accepts third-party track feeds via an open API, or what certification that requires.
- No confirmation whether MAVLink or AuterionOS app policies allow custom high-rate entity messages on the serial link.
- No sources on the SAR, disaster-response or maritime buyers' bandwidth constraints. These dual-use claims are inference.
