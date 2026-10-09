# Hackathon plan: D4D x EDTH Seoul

Status: v0.1, written 2026-10-09 (Builders Night). Event: D4D x EDTH, Seoul, build 10 Oct, judging
11 Oct 2026. Bringing existing code is allowed. Background and sources:
[`reports/EDTH and D4D lessons for MinBand.md`](../reports/EDTH%20and%20D4D%20lessons%20for%20MinBand.md); cross-domain findings and the
stretch review, 2026-10-09:
[`reports/Drone navigation challenges for MinBand.md`](../reports/Drone%20navigation%20challenges%20for%20MinBand.md).
Hardware on hand: iPhone (LiDAR; the iOS app is tested on device), laptop, Raspberry Pi 5. No
external radio yet.

Work top to bottom. P0 is the demo; P1 makes it land with the military judges; stretch items are
picked up only once everything above them works end to end and is rehearsed.

## 1. What we claim (differentiators)

One line: **bandwidth proportional to surprise.** A few hundred B/s instead of megabits of video,
and the picture survives the link dying.

| # | Claim | Proof on stage |
|---|---|---|
| 1 | The edge runs the receiver's predictor (a ghost) and sends only when the receiver would be wrong. | Bytes graph flat while a walker moves in a straight line, spikes on turns |
| 2 | The predictor is bit-identical across platforms (f32 basic ops, integer ticks). | Golden vectors pass native, WASM, and on the Pi 5 (§4) |
| 3 | Fidelity degrades continuously under a byte budget instead of failing. | Step the link box down through profiles; the "fidelity knob" readout rises |
| 4 | Loss is repaired by resending current state; updates are idempotent; no retransmission queue. | Contested profile: twin stays coherent at 10-20 % loss |
| 5 | Blackouts are visible and honest: entities keep predicting and age visibly, then re-sync within one keyframe (2 s). | Pull the Ethernet cable on the link box, plug it back |
| 6 | Several devices fuse into one picture. | Phone plus sim devices on one link (§4) |
| 7 | Numbers are measured: fidelity-vs-bytes curve, two baselines, loss sweep. | `runs/eval/*.svg`, measured H.264 (P0) |
| 8 | It is a link layer, not a drone: radio- and airframe-neutral, sits under or beside video. | Drones-per-link slide: ~10 one-walker feeds in 9.6 kbit/s |

Demo order: straight line and a turn, then pull the cable on the clean or hf profile, plug it back,
then step the box down through hf, lora, telemetry. After a slow profile the twin keeps the slow
heartbeat's thresholds for one coast period (about 19 s after telemetry), so a cable pulled right
after stepping back up shows coasting late; on telemetry itself coasting starts after ~19 s of
silence and stale at 45 s, by design (15 s heartbeat). `cd e2e && npm run record` records this
order as the fallback run.

Do not claim: that the delta idea is new (it is DIS dead reckoning, say so first); the detector
(edge detection is crowded; lead with the link and the twin); stealth (fewer transmissions mean
less airtime, not undetectable); the video ratio before H.264 is measured on the phone.

## 2. Weekend plan

| Pri | Item | Where | Done when |
|---|---|---|---|
| P0 | Pick the track (Tactical Edge: Sensor Fusion & Edge AI, else UAS/C-UAS), quote its problem statement on slide 1, get one concrete scenario and real link rates from a military mentor | slides | Scenario written on slide 1 |
| P0 | Measure H.264 on the phone (VideoToolbox) at 720p/480p/360p | `runs/baseline_a.json`, `tools/eval` | Viewer overlay no longer says "configured" |
| P0 | Pi 5 link box with profiles | §3, `tools/pi-link.sh` (scripted; verify on the Pi) | Phone -> Pi -> laptop works; each profile changes the bytes graph |
| P0 | Budget fixes needed for profiles below ~4 kbit/s, including keyframe cadence from the budget (S19) and keyframe pacing (S16) | §3.4 | Telemetry and LoRa profiles hold a static scene without saturating or dropping the keyframe burst |
| P0 | Record a fallback run; rehearse the 3 + 2 min and 5 min versions | `runs/eval` | Video file and slides frozen |
| P1 | Visuals V2, V1, V3 (§5): V2 fed by the threshold byte and the coasting state (S14, S15), V1 with the thumbnail competitor (S23) | core, server, viewer | Each rehearsed in the demo script |
| P1 | CoT export to ATAK/iTAK/WinTAK: one CoT event per fused entity, affiliation unknown (`a-u-G...`), `ce`/`le` from the threshold byte (S14) grown with age, `stale` from staleness, `how` from the provenance state (S4); marker lat/lon/heading from config (S3 folds in here) | new `server/src/cot.ts` next to `world.ts`, `fusion.ts` | Entity appears on a TAK screen with a `ce` a consumer can use. *Status 2026-10-09: built and tested against loopback UDP/TCP (`MINBAND_GEO`, `MINBAND_COT`, `/api/geo`, `/api/cot`, `server/README.md`); `ce` reads "unknown" until fusion fills `GlobalEntity.ce` (S14); `how` is `m-p`/`m-f` until S4; not yet seen on a real TAK screen* |
| P1 | Drones-per-link: N sim devices from the Pi plus the phone through the HF profile, with time-on-air beside bytes (S2) | §4, `server/src/sim.ts`, server metrics | Per-device and total B/s, and the share of channel airtime, visible under 9.6 kbit/s |
| P1 | Pi 5 as a further platform for the golden vectors | §4 | `cargo test` green on the Pi |
| P2 | Reconcile the per-Update size (`proto/PROTOCOL.md` ~31 B vs `docs/DESIGN.md` ~22 B) and relabel person as dismount in the UI | docs, viewer | One number in every slide and doc |

## 3. Raspberry Pi 5 link box

The Pi sits physically between the phone and the laptop and is "the radio": Linux shapes rate,
delay and loss at OS level, outside our own server, so the demo is not grading its own homework.
It also gives us a private network, so venue Wi-Fi does not matter (DESIGN §8 risk). The
in-process shaper and its "blackout 10 s" preset stay as the fallback.

```
iPhone ──Wi-Fi (Pi hotspot)──► Raspberry Pi 5 ──Ethernet──► Laptop (server + viewer)
           uplink shaped on eth0 egress, downlink (acks) shaped on wlan0 egress
```

The laptop must be on Ethernet, not on the hotspot: traffic between two Wi-Fi clients of the same
AP is forwarded inside the Wi-Fi stack and never passes `tc`.

**Scripted** in [`tools/pi-link.sh`](../tools/pi-link.sh): `setup`, the profiles, `contested`,
`status`, `clear`, `--dry-run`; usage in [tools/README.md](../tools/README.md#pi-5-link-box-pi-linksh).
The commands below are what it runs and stay as the reference. Its tc tree and filters are tested
on a Linux kernel with pfifo in netem's place (`tools/test/`); netem itself and `nmcli` are not
(no Pi in the dev environment), so verify each step on the Pi before relying on it.
`sudo tools/test/pi-link-kernel.test.sh` on the Pi also checks the real netem.

### 3.1 Hardware and OS

- Pi 5, onboard dual-band Wi-Fi (no external antenna needed), Ethernet cable to the laptop (USB-C
  Ethernet adapter on a Mac). Power: official 27 W USB-C supply, or a USB-C PD power bank so the
  box can be carried around on stage.
- Raspberry Pi OS Bookworm 64-bit (NetworkManager). Set the WLAN country to KR
  (`sudo raspi-config` -> Localisation -> WLAN Country) or 5 GHz AP mode will not start.

### 3.2 Network

```bash
# Hotspot for the phone. NetworkManager "shared" mode gives DHCP, forwarding and NAT; Pi = 10.42.0.1.
# band a = 5 GHz (venue 2.4 GHz is crowded); use band bg if the phone does not see it.
sudo nmcli device wifi hotspot ifname wlan0 con-name minband-ap ssid minband-link band a password '<8+ chars>'

# Static Ethernet to the laptop. Laptop: 192.168.77.2/24, no gateway (macOS: Network -> Ethernet
# adapter -> Details -> TCP/IP -> Manually).
sudo nmcli con add type ethernet ifname eth0 con-name minband-eth \
  ipv4.method manual ipv4.addresses 192.168.77.1/24
sudo nmcli con up minband-eth
```

Scripted: `sudo tools/pi-link.sh setup --password '<8+ chars>'` (also sets autoconnect, so the
box comes back after a reboot).

Point the iOS app at `192.168.77.2:7777`. Shared mode should masquerade the phone's traffic, so
the server sees it from `192.168.77.1`; check with `curl localhost:8080/api/metrics` on the
laptop (`devices[].addr`). If nothing arrives, add a return route on the laptop
(`sudo route -n add 10.42.0.0/24 192.168.77.1` on macOS) and allow incoming connections for
`node` in the macOS firewall.

### 3.3 Profiles

Only UDP :7777 goes through `netem`; SSH and everything else bypass it. `limit` is a small queue
(packets) like a real radio's buffer, so excess traffic is dropped instead of building seconds of
latency. tc units: `bit` = bit/s (`bps` would mean *bytes* per second).

```bash
# shape <dev> <dport|sport> <netem args...>
shape() {
  dev=$1; m=$2; shift 2
  tc qdisc del dev "$dev" root 2>/dev/null || true
  tc qdisc add dev "$dev" root handle 1: prio bands 4
  tc qdisc add dev "$dev" parent 1:4 handle 40: netem "$@"
  tc filter add dev "$dev" parent 1: protocol ip prio 1 u32 \
    match ip protocol 17 0xff match ip "$m" 7777 0xffff flowid 1:4
}
shape eth0  dport rate 2kbit delay 300ms loss 10% limit 4   # uplink, phone -> laptop
shape wlan0 sport rate 2kbit delay 300ms loss 10% limit 4   # downlink, acks -> phone
# clear: tc qdisc del dev eth0 root; tc qdisc del dev wlan0 root
```

| Profile | Rate | One-way delay | Loss | Queue | Edge budget (`/api/budget?bps=`) | Stands for |
|---|---|---|---|---|---|---|
| `clean` | none | none | 0 | none | 0 | Wi-Fi reference |
| `degraded` | 64 kbit | 20 ms | 2 % | 20 | 0 | Busy mesh |
| `hf` | 9600 bit | 500 ms | 1 % | 32 | 8000 | NATO HF ceiling (drones-per-link slide) |
| `lora` | 2 kbit | 300 ms | 10 % | 4 | 1500 | Meshtastic-class LoRa |
| `telemetry` | 600 bit | 50 ms | 5 % | 4 | 450 | ELRS-class control-link telemetry |
| `contested` | `lora`, alternating with 1-5 s random blackouts | | | | 1500 | Intermittent jamming |
| `blackout` | | | 100 % | | | Link cut (or pull the Ethernet cable) |

Scripted: `sudo tools/pi-link.sh <profile>`, which prints the budget command for the profile.
Three details differ from the sketch above: netem's rate gets a `-14` B packet overhead, since at
the qdisc a datagram still carries its 14 B Ethernet header and the server and `tools/eval` count
payload + 28 B; `clean` keeps the tree with a pass-through netem so `status` still counts; and
`hf`'s queue is 32, not 8 (below).

When switching profiles on the box, tell the server which one it emulates:
`curl 'localhost:8080/api/link?profile=external&as=lora'` sets the edge budget from this table and
the airtime model for the time-on-air readout, with the in-process shaper off (stretch L2 is the box
doing this itself). Without the box, `/api/link?profile=lora` applies the same row in-process,
including `contested` as a server-side loop (`server/README.md`). The budget is the link's: the
server splits it over the devices it hears. Unplugging the cable can change the NAT source
port when it comes back; the server's device identity handles that (DESIGN §4).

netem's `limit` counts packets still in the delay line, so it also caps the rate in datagrams:
`limit / delay`. With the sketch's 8 that is 16 datagrams/s for `hf`; eight one-walker feeds send
about 17/s, so a third of them were dropped at the queue (measured in-process, which models `limit`
the same way). A real radio's buffer does not hold packets in flight, so the `hf` row uses 32
(about 8 % drops in the same run, mean twin error 4.4 cm).

Say what the box is: it reproduces a radio's rate, delay and loss, not its framing. At 600 bit/s
the 28 B UDP/IP header is a large share of every datagram; a real telemetry radio would not carry
it.

### 3.4 Budget fixes before the low-rate profiles (P0)

The budget controller can only widen thresholds; three traffic sources sit outside it.

1. **Pose.** iOS sends `Pose` at a fixed 2 Hz (`ios/MinBand/Pipeline.swift`, `poseInterval`).
   At ~40 B payload plus 28 B header that is ~1 kbit/s on the wire by itself, more than the whole
   `telemetry` profile. Fix: derive the pose interval from the budget (e.g. 10 s, or off, below
   4 kbit/s). The frustum is cosmetic. Derive `keyframe_ticks` and `hello_refresh_ticks` in the
   same place (S19): 2 s at 8 kbit/s and above, down to ~15 s at 600 bit/s. The heartbeat period
   is a link-class parameter everywhere else: DIS uses 5 s, Iridium SBD carries one 340 B message
   per 10-15 s.
2. **Header.** The controller counts payload only (`core/src/edge.rs`, `emit` adds `b.len()` to
   `window_bytes`), while the link, the server metrics and `tools/eval` count +28 B per datagram.
   Fix: count the header in `window_bytes` so the controller targets what the link sees.
3. **Floor.** Keyframes every 2 s and the 5 s Hello refresh are not budgeted. The static scene's
   floor is 61.5 B/s on the wire (`docs/EVAL_FINDINGS.md`), about 490 bit/s, so `telemetry` at
   600 bit/s only fits small scenes until the compact codec (stretch S6) lands. Expect that and
   say it on stage.
4. **Keyframe burst.** A keyframe goes out as several parts in one tick. At 2 kbit/s a 160 B
   keyframe is ~0.6 s of link time against the 4-packet `netem` queue, so parts are dropped and
   the floor looks worse than it is. Fix: pace the parts, one per ack interval (S16);
   `reconcile_keyframe` in `core/src/receiver.rs` already tolerates parts arriving over time.

After 1, 2 and 4, rerun `cd core && cargo test` and `cd tools/eval && npm run eval`; regenerate the
golden file only if the change is intentional (`UPDATE_GOLDEN=1`).

### 3.5 Link box polish (stretch)

| # | Idea | Size |
|---|---|---|
| L1 | `tools/pi-link.sh <profile>` wrapping §3.2-3.3, plus `contested` as a background loop (done) | S |
| L2 | The script also sets the edge budget and reports the profile name to the server so the viewer shows `LINK: lora 2 kbit/s · 68 % airtime` (`/api/link?profile=external&as=<profile>` exists and returns the airtime figure (S2); the keyframe period follows the budget with S19) | S |
| L3 | Physical button on the Pi GPIO (gpiozero) that cycles profiles; an LED that goes dark on blackout | S |
| L4 | `simplex` profile: downlink 100 % loss, so the ground station never transmits (do together with S1, the first stretch item) | S |

## 4. Raspberry Pi 5 as an edge

No camera needed for the first two steps.

```bash
# Golden vectors on Linux aarch64: the same predictor, bit for bit, on companion-computer-class hardware.
curl --proto '=https' -sSf https://sh.rustup.rs | sh
cd core && cargo test
# Without a Pi: the same tests cross-compiled for aarch64 Linux, run under qemu-user.
tools/golden-aarch64.sh

# Sim edge(s) on the Pi -> laptop through the link box. Copy core/pkg-node from the laptop
# (WASM is platform-independent) instead of installing wasm-pack on the Pi. Node 20+.
cd server && npm install
MINBAND_HOST=192.168.77.2 DEVICES=8 npm run sim
```

Sim traffic leaves through `eth0`, so its uplink is shaped; its acks arrive on `eth0` ingress and
are not. Use it for the drones-per-link run: eight simulated devices plus the phone sharing the
`hf` profile.

## 5. Visual presentation

Each visual proves one claim. Dramatic ones live behind a `STAGE` toggle so the operator view
keeps the restraint rules in `docs/STYLE.md`; all of them stay monochrome, hairline, and move
only when something happened.

| # | Visual | Proves | Where | Size |
|---|---|---|---|---|
| V1 | **Video on the same link.** Beside the live twin, the halftone panel shows what video would deliver through the current profile: frames paint in line by line at the link rate (a ~30 KB still takes ~2 min at 2 kbit/s), with `NEXT FRAME 1:52`. Labelled as computed from the measured bitrate. Beside it the honest competitor, `AI THUMBNAIL 150 B EVERY 2.5 s` at the current rate, computed from the budget (S23), so the panel sees that comparison before a judge raises it. | ~1/1000 of the bytes, and ahead of a thumbnail feed | `viewer/src/halftone.ts`, side-by-side panel | S-M |
| V2 | **Uncertainty rings in a blackout.** Each entity gets a hairline ground ring that widens with time since its last update; on reconnect the rings snap to points. The radius starts at the entity's last self-declared threshold (S14) and grows with age and the class speed prior; after one missed keyframe (2 s) the entity is drawn as coasting, before stale at 6 s (`stale_ticks` in `core/src/receiver.rs`; S15). Pairs with pulling the Pi's Ethernet cable. | Survives the link dying, honestly | `viewer/src/scene.ts` | S |
| V3 | **Packet waterfall and click.** A `LINK ACTIVITY` strip: time scrolls down, one tick per datagram, width = bytes (video would be a solid bar). Optional soft click per packet, like a Geiger counter: silent while predictable, clicks on turns. | Bandwidth proportional to surprise | structured per-datagram WS event (device, kind, bytes, ids) in `server/src/world.ts` (today it is only a text log line); viewer strip | M |
| V4 | **Tolerance bubble on the phone.** In the iOS `WIREFRAME` stage mode, a wire sphere of radius θ_pos x theta_scale sits on each person's ghost; walking stretches it, leaving it snaps it back and a packet goes. Needs an FFI accessor returning ghost positions predicted to `now` (ghosts are in `core/src/edge.rs`, `theta_scale` is already in stats). | The mechanism, without words | `core/src/edge.rs`, `core/src/ffi.rs`, `ios/MinBand/ARViewContainer.swift` | M |
| V5 | **Packets in the twin.** Using V3's event, each update draws a short line from the device frustum to the entity it corrects, with the error that triggered it (`+17 cm`). | Only surprises are sent | `viewer/src/scene.ts` | M |
| V6 | **Byte odometers.** `MINBAND 48 KB` vs `VIDEO 112 MB` since demo start, tabular digits, live ratio. | The headline number | viewer credits row | S |
| V7 | **Eight drones, one HF link.** Eight sim devices from the Pi through `hf`, each with a moving frustum over the terrain, fused into one picture. The sim sends `Pose` for each device (`SCENE=spread`: a drone orbit over its area). On LoRa-class links the limit is airtime, so say `N feeds fit in X % of the channel` (S2). | Drones per link | `server/src/sim.ts` | S-M |
| V8 | **Live point on the eval curve.** The fidelity-vs-bytes chart with a dot that slides as the profile steps down. | Measured, not claimed | viewer, `runs/eval/fidelity_vs_bytes.csv` | M |

Props: a 128x64 monochrome OLED on the Pi showing profile and kbit/s plus a `JAM` toggle switch
(with L3); the phone on a pole or filmed from a mezzanine for a drone-like view; slides in the
same visual language, reusing the eval charts.

Order for the weekend: V2 (with S14 and S15), V1 (with S23), V3, then V4 (the iOS app is tested,
so it is safe to build on). V2 goes first because it is the most-precedented visual in the plan:
ADS-B decoders coast and then drop, ASTERIX tracks carry a coasted bit, and the FAA grounded
displays that coasted "with no warning to the crew" in 2017 (§7).

## 6. Stretch ideas, in order

Re-ordered 2026-10-09 after the drone-navigation research (report sections "Stretch review" and
"New stretch ideas"). Seven items moved up into P0/P1 above because they make the demo's existing
claims true rather than add new ones: S19 cadence from budget and S16 keyframe pacing (with the
§3.4 fixes), S14 threshold byte and S15 coasting state (with V2), S23 thumbnail baseline (with
V1), S2 airtime (with drones-per-link), S3 geodetic anchor (with CoT). They are specified in §6.1.

Everything below starts only after the fallback run is recorded and rehearsed. Pick from the top;
each is a vertical slice that can be demoed on its own.

| # | Idea | Where | Size | Why it scores |
|---|---|---|---|---|
| S1 + L4 + S9 | **Quiet / simplex mode with an emission policy.** Pre-provisioned edge that streams without waiting for an `Ack`; server option to never ack; `simplex` profile on the box (downlink 100 % loss). Repeat state once or twice right after a change, then back off to the heartbeat (the IEC 61850 GOOSE pattern); jitter the keyframe and Hello periods so the 2 s / 5 s fingerprint goes. Today the edge sends only `Hello` until acked and the server acks up to 10x/s, so the ground station transmits about as often as the drone. | `core/src/edge.rs`, `ios/MinBand/Pipeline.swift`, `server/src/world.ts`, `tools/pi-link.sh` | M (~3 h) | Operators are geolocated by their emissions (CRFS, Kvertus balloon SIGINT, handheld detectors); the first question from a judge who has read about Rubicon. The eval harness already measures the no-ack cost (the "repair off" trace) |
| S22 | **Receiver plausibility gate.** Flag an update whose implied speed exceeds the class `max_speed`, or whose jump exceeds `max_speed x dt + 3θ`, as `doubtful` instead of applying it blindly; keep the rejects so a track anchored on a bad update can recover. | `core/src/receiver.rs` (`apply`), `viewer/src/scene.ts` | S (~2 h) | The two-hour answer to "can the feed be spoofed": ADS-B has no authentication and survives by exactly this gate (as implemented in pyModeS 3.6.0: reject positions implying > 1,500 kt with a 2 km margin over the last 5 positions) |
| S17 | **Per-entity minimum interval and hold rule.** No second delta for one entity within 100 ms unless the error exceeds 2θ; after a triggered delta, hold the trigger sensitivity for 3 evaluations. | `core/src/edge.rs` trigger; check on the noisy-walker scenario in `tools/eval` | S (~1-2 h) | ETSI CAM generation rule (100 ms minimum, 1 s maximum, hold for 3 messages; from memory, standard cited in the report); the noisy walker costs 168 B/s against 118 B/s clean (`docs/EVAL_FINDINGS.md`) |
| S20 | **Reporter election in fusion.** Per source `tq = conf x decay(age)`; the fused entity reports the best source and switches only when another beats it by a margin for `MERGE_MS`; single-source groups shown `tentative`. | `server/src/fusion.ts`, `server/src/cot.ts` | S-M (~2-3 h) | Link 16 reporting responsibility; dual tracks "undermine the confidence of human operators" (JHU APL). Two phones with 1 m registration error produce dual tracks at the 0.5 m merge distance; only needed if two phones are on stage |
| S13 | **Utility test, shrunk.** Three conditions on the recorded run at equal bytes: twin, halftone video at link rate, AI thumbnail every N s; an operator reports count, class and location; one decoy condition. | `tools/eval`, a script for the test | M (~3-4 h) | DARPA scores reduction "with preserved mission utility"; the thumbnail is the honest competitor, and no source anywhere quantifies false identification against decoys, so a measured number is rare |
| S6 | **Compact codec tier.** A second message kind, not just smaller numbers: bit-packed fixed point relative to a tile origin with an epoch, quantised velocity, an explicit validity rule for the reference, 8-16 B per entity; measured against postcard in `tools/eval`. | `core/src/wire.rs`, `tools/eval` | M-L (6-10 h) | AIS has a 168-bit and a 96-bit tier (0.185 m vs 185 m resolution); ADS-B CPR positions are valid only against a reference less than 10 s old; MAVLink `HIGH_LATENCY2` is 42 B. Say "8-16 B against a median 87 B Meshtastic TAK packet that also carries uid and callsign", not "4-8x denser than CoT". Needed before LoRa / ELRS-class links, where today's 60 B/s floor does not fit; if unbuilt, present it as the next step with those numbers |
| S8 | **Image chip on demand, shrunk.** Operator clicks an entity; the edge sends one 32x32 or 64x64 JPEG in budget-sized parts, progressive (`Hello.caps` bit1 is reserved). At 60 B/s a 150 B chip fits every 2.5 s; a 2 KB JPEG takes ~30 s. | core, iOS, server, viewer | M-L | Every fielded system that reached a decision-maker pairs the cue with evidence: Saildrone sends one clearest image per 15-20 min so the satellite link and the command centre are not flooded (USCG); Delta pairs each AI cue with imagery and a human review; MeshCore ships 100-200 B thumbnails over LoRa |
| S4 | **Provenance state, then confirm / reject.** Each fused entity carries `unconfirmed` / `seen by 2 sensors` / `operator-seen`, shown in the twin and carried into CoT `how`; the confirm button is enabled only once S8 exists, otherwise it is a guess with a button. | `server/src/world.ts`, `server/src/cot.ts`, viewer | S | Human in the loop; decoys are common; answers the objection to confirming without imagery. The state part can go into P1 CoT |
| S7 | **Compact authenticated encryption.** Pre-shared per-session key, 4-byte counter as implicit nonce, 64-bit tag (Ascon-AEAD128 or AES-CCM via a vetted crate, no custom crypto); the receiver persists the counter high-water mark; IDs and pose stay inside the ciphertext. If time is short, a slide with these numbers scores more than half-built code. | new `core/src/crypto.rs` | M (4-6 h) | 802.15.4 MIC-64 plus a 4 B frame counter is 12 B; NIST SP 800-232 keeps tags at 64 bits or more; MAVLink 2 signing is 13 B and its replay defence fails when the timestamp is not persisted across reboots (PX4 docs). For ROK judges: "a KCMVP-compatible boundary, key management out of scope" |
| S18 | **Keyframe request bit.** `Ack` gains a "send keyframe now" flag for gaps older than `gap_forget_ticks` or after a blackout, replacing long `missing` lists. | `core/src/wire.rs` (`Ack`), `core/src/receiver.rs` (`make_ack`), `core/src/edge.rs` (`on_ack`) | S | RTP PLI/FIR and market-data snapshot channels (from memory, unsourced); partly present already (keyframe when more than 8 seqs are missing) |
| S21 | **Blackout summary on reconnect.** The first keyframe after a silence longer than `keyframe_ticks` carries counts of entities spawned and despawned meanwhile and the largest displacement; viewer shows `WHILE LINK WAS DOWN: +2 -1`. | `core/src/edge.rs` (despawns are remembered 10 s), `core/src/wire.rs`, viewer | S-M | Wildlife tags summarise the blackout window instead of replaying samples; Delta names "stale coordinate" as its failure mode |
| S12 | **Real low-rate radio.** Pair of Meshtastic LoRa nodes (KR920 in Korea) or a SiK telemetry pair; a measured run replaces the emulated profiles. | `tools/` | L (hardware) | Measured beats emulated. Stock Meshtastic delivers 5-20 B/s, below the 60 B/s floor: without S6 the honest result is "does not fit", still worth showing as the measured floor |
| S10 | **MAVLink over serial, no radio needed.** Wrap datagrams in MAVLink 2 (`TUNNEL`, verify) over the Pi 5 UART to the laptop through a USB-serial adapter at 57600 baud. | new transport in server and a Pi bridge | M | The path onto the serial links drones already carry (Skynode-class); only if an adapter is at hand |
| S5 | **Dismount relabel and vehicle motion prior only.** Label person as dismount; add a vehicle prior in `classes.rs` for the sim. Do not add a COCO vehicle filter to the detector or claim it: COCO "truck" is not a BMP, decoys defeat low-resolution class labels, and DARPA's call excludes fixed-category detection. | `core/src/classes.rs`, viewer | S | Judges map it to their own targets; the pitch leads with the link, not the detector |

Dropped for the weekend: S11 Pi 5 camera edge (the golden vectors on the Pi already prove
portability; see §8) and L3 beyond a stage prop.

### 6.1 Items promoted into P0/P1

| # | Idea | Where | Size | Why |
|---|---|---|---|---|
| S14 | **Self-declared threshold byte.** Add `theta_q: u8` (quantised θ_pos x `theta_scale`) to `Delta` and `Keyframe`; the receiver exposes it per entity; the server computes `ce = θ + growth(age, class max_speed)`; V2 draws the ring from it; `cot.ts` fills `ce`/`le`. Regenerate the golden file (intentional change). | `core/src/wire.rs`, `core/src/edge.rs` (`emit`), `core/src/receiver.rs` (`Extrapolated`), `server/src/world.ts`, `viewer/src/scene.ts`, `core/tests/golden` | S-M (~3 h) | Every mature state broadcast carries its own error class in a few bits: ADS-B NIC/NACp/SIL, MAVLink `HIGH_LATENCY2` `eph`/`epv` ("max error since last message", one byte each), Link 16's 4-bit track quality because a covariance is 21 numbers. MinBand sends detector confidence but never its active threshold, so the ring and CoT `ce` cannot be honest today |
| S15 | **Coasting state and link-good tightening.** The receiver marks a device's entities `coasting` after one `keyframe_ticks` (2 s) without any datagram, before `stale` at 6 s; while the last ack is fresh, the ring shrinks toward θ on each silent tick; a missed keyframe is a visible event in V2/V3. | `core/src/receiver.rs` (new `coast_ticks` beside `stale_ticks`), `server/src/world.ts` (`DEVICE_SILENT_MS` is 5 s today), `viewer/src/scene.ts` | S (~2 h) | Under a prediction-error trigger, silence is information only while loss is excluded, so the keyframe is the trust mechanism and its absence must change the picture. Coasting without warning has a record: FAA AD 2017-22-14 (five incidents), a 10 s Blue Force Tracker lag raising friendly engagements in a lab study |
| S16 | **Keyframe pacing.** Emit keyframe parts one per ack interval instead of in one tick; optionally jitter the period by 20 % (S9). | `core/src/edge.rs` keyframe block; `reconcile_keyframe` already tolerates parts arriving over time | S (~1-2 h) | §3.4 item 4: the burst, not the bytes, breaks the 2 kbit/s profiles (video codecs spread the refresh for the same reason) |
| S19 | **Cadence from budget.** `set_budget` derives `keyframe_ticks` (2 s at 8 kbit/s and above, ~15 s at 600 bit/s), `hello_refresh_ticks` and the pose interval; L2 shows the resulting period. | `core/src/edge.rs` (`set_budget`), `ios/MinBand/Pipeline.swift` (`poseInterval`) | S (~1 h) | §3.4 item 1: the heartbeat period is a link-class parameter (DIS 5 s, Iridium SBD one message per 10-15 s) |
| S23 | **Thumbnail baseline.** Add "AI thumbnail every N s at this rate" as a third baseline in `tools/eval`, in V1 and in V6, with N computed from the budget and a 150 B chip. | `tools/eval/src/baselines.ts`, `viewer/src/halftone.ts` | S (~1-2 h) | The honest competitor at these rates is not video but a periodic thumbnail (MeshCore 100-200 B images over LoRa; Saildrone's throttled images); show it before a judge asks |
| S2 | **Time-on-air readout.** Datagrams/s and time on air per profile next to bytes/s, from the Semtech LoRa formula for `lora` and from the serial rate for `telemetry`/`hf`. | server metrics (`msgsPerSec` exists), viewer panel | S (~2 h) | On LoRa and mesh the budget is airtime: 16 B costs 354 ms on Meshtastic LongFast, so the 60 B/s floor is roughly 70 % of a LongFast channel (our calculation, a LongFast figure); Silvus's 559-node mesh spent under 35 % of airtime on position reports. The `lora` profile (and `external&as=lora`) models MediumSlow (SF10, ~1.95 kbit/s raw) to match its 2 kbit/s rate, where 16 B costs 198 ms and the same traffic needs about half the airtime |
| S3 | **Geodetic anchor.** Marker lat/lon/heading in config; the server converts twin positions to WGS84 and MGRS; the viewer shows the grid reference. | new `server/src/geo.ts`, viewer | S | No C2 system takes marker-frame metres; part of P1 CoT. *Server side done 2026-10-09: heading = true bearing of the marker's -Z (toward the image's top edge); `Snapshot.geo`, `GlobalEntity.geo` carry lat/lon/MGRS* |

## 7. Rules borrowed from other domains

The research's main result: every mature "send only what changed" system added two things MinBand
still lacks, a per-message error bar and a visible coasting state, and on shared radios it
measures airtime, not bytes. The report tags each finding [V] (read from an open-source
implementation of the standard, quotable as "as implemented in ...") or [U] (from memory, with
the standard named, unverified); the full list with sources is in the report's "non-obvious
findings" table.

| Rule | Where it comes from | Use here |
|---|---|---|
| Silence is information only while loss is excluded; the heartbeat is what makes "no update = within threshold" true | Event-triggered estimation (Trimpe and D'Andrea 2012; Wu et al. 2013) | "A missed keyframe changes what the operator sees" (S15, V2) |
| Every state message carries its own error class in a few bits | ADS-B NIC/NACp/SIL [V, pyModeS 3.6.0]; MAVLink `HIGH_LATENCY2` `eph`/`epv` [V, pymavlink 2.4.50]; Link 16 4-bit track quality | Threshold byte (S14), CoT `ce`/`le` |
| Coast, mark, then drop; never extrapolate silently | FAA AD 2017-22-14; Blue Force Tracker 10 s lab study; pyModeS 30 s / 300 s windows [V] | V2 first; coasting at 2 s, stale at 6 s |
| On shared radios the budget is airtime | 16 B = 354 ms on LongFast; Silvus 559-node mesh < 35 % airtime for position reports | S2, V7, drones-per-link |
| A coarser tier is a separate message with an explicit validity window | AIS msg 27 (96 bits) vs msg 1 (168 bits) [V, pyais 3.3.0]; ADS-B CPR reference < 10 s old [V] | S6 design |
| The heartbeat period is a link-class parameter | DIS 5 s; Iridium SBD one 340 B message per 10-15 s | S19 |
| A minimum interval and a hold rule stop flapping | ETSI CAM: 100 ms minimum, 1 s maximum, hold for 3 messages [U] | S17 |
| Repeat after a change, back off, expire late data instead of delivering it | IEC 61850 GOOSE [U]; DTN Bundle Protocol v7 lifetime [V, py-dtn7] | S1/S9 repair policy |
| One reporter per track, switch only with a margin | Link 16 reporting responsibility [U]; dual tracks undermine operator confidence (JHU APL) | S20 |
| Unauthenticated broadcasts survive by plausibility gating | pyModeS motion-consistency check [V] | S22 |
| Rate-limit the evidence, not the cue | Saildrone: one image per 15-20 min (USCG) | S8 |
| Change-triggered senders have no rate ceiling without a controller | Event cameras ship rate controllers | Say on stage: manoeuvring targets and decoys collapse the savings toward keyframe-only rates |

For ROK judges, the sourced numbers (report, "What to say to ROK judges"): the December 2022
incursion, first radar track at 10:25, about six minutes to classify, five drones airborne about
five hours, none downed; 29 May to 2 June 2024 jamming, 201 aircraft and 731 ships affected by
31 May (MSIT count); ICAO: interference in the Incheon FIR ongoing since 2 October 2024; a Navy
S-100 lost off Yeonpyeong in April 2024, attributed to jamming by a lawmaker; TICN gives high-rate
links only to fixed stations, not on the move, and TICN-II adds aerial relay drones; KVMF is
bit-packed VMF, so a bit-packed tier (S6) is the local idiom; the 2027 drone and counter-drone
line is KRW 806.1 bn. Do not quote the single-source "39 to 578 incidents" figure. If asked for
ROK data-link requirements, say "we found none published".

## 8. After the hackathon

Geodesy with real error bars, global track IDs and a shared timebase, porting off ARKit (VIO,
rangefinder or terrain ray-cast), thermal, and the Ukraine and NATO pathways are in the first
report's "Fielding" section and its months table. The second report adds the framing for the next
build: the state channel and the evidence channel are different products that drone warfare
cannot combine, so the layer that paces both is the product. Order: S6 compact tier and S8 chips,
then S11 Pi 5 camera edge (Camera Module 3, detector on the AI HAT+ or low-res CPU YOLO, flat-ground
positions; DARPA's 2-5 W edge budget is the argument), then the KVMF / Link-K export question for
ROK users. Next event on the list: EUDIS autumn hackathon, 15-17 Oct 2026.
