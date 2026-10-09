# Hackathon plan: D4D x EDTH Seoul

Status: v0.1, written 2026-10-09 (Builders Night). Event: D4D x EDTH, Seoul, build 10 Oct, judging
11 Oct 2026. Bringing existing code is allowed. Background and sources:
[`reports/EDTH and D4D lessons for MinBand.md`](../reports/EDTH%20and%20D4D%20lessons%20for%20MinBand.md).
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

Do not claim: that the delta idea is new (it is DIS dead reckoning, say so first); the detector
(edge detection is crowded; lead with the link and the twin); stealth (fewer transmissions mean
less airtime, not undetectable); the video ratio before H.264 is measured on the phone.

## 2. Weekend plan

| Pri | Item | Where | Done when |
|---|---|---|---|
| P0 | Pick the track (Tactical Edge: Sensor Fusion & Edge AI, else UAS/C-UAS), quote its problem statement on slide 1, get one concrete scenario and real link rates from a military mentor | slides | Scenario written on slide 1 |
| P0 | Measure H.264 on the phone (VideoToolbox) at 720p/480p/360p | `runs/baseline_a.json`, `tools/eval` | Viewer overlay no longer says "configured" |
| P0 | Pi 5 link box with profiles | §3, new `tools/pi-link.sh` | Phone -> Pi -> laptop works; each profile changes the bytes graph |
| P0 | Budget fixes needed for profiles below ~4 kbit/s | §3.4 | Telemetry and LoRa profiles hold a static scene without saturating |
| P0 | Record a fallback run; rehearse the 3 + 2 min and 5 min versions | `runs/eval` | Video file and slides frozen |
| P1 | Visuals V1-V3 (§5) | viewer, server | Each rehearsed in the demo script |
| P1 | CoT export to ATAK/iTAK/WinTAK: one CoT event per fused entity, affiliation unknown (`a-u-G...`), `ce`/`le` from the error estimate, `stale` from staleness; marker lat/lon/heading from config | new `server/src/cot.ts` next to `world.ts`, `fusion.ts` | Entity appears on a TAK screen |
| P1 | Drones-per-link: N sim devices from the Pi plus the phone through the HF profile | §4, `server/src/sim.ts` | Per-device and total B/s visible under 9.6 kbit/s |
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

The commands below are an untested sketch (no Pi in the dev environment); verify each step on
the Pi before relying on it.

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
| `hf` | 9600 bit | 500 ms | 1 % | 8 | 8000 | NATO HF ceiling (drones-per-link slide) |
| `lora` | 2 kbit | 300 ms | 10 % | 4 | 1500 | Meshtastic-class LoRa |
| `telemetry` | 600 bit | 50 ms | 5 % | 4 | 450 | ELRS-class control-link telemetry |
| `contested` | `lora`, alternating with 1-5 s random blackouts | | | | 1500 | Intermittent jamming |
| `blackout` | | | 100 % | | | Link cut (or pull the Ethernet cable) |

Set the budget from the laptop with `curl 'localhost:8080/api/budget?bps=...'` when switching
profiles, until the box does it itself (stretch L2). Unplugging the cable can change the NAT source
port when it comes back; the server's device identity handles that (DESIGN §4).

Say what the box is: it reproduces a radio's rate, delay and loss, not its framing. At 600 bit/s
the 28 B UDP/IP header is a large share of every datagram; a real telemetry radio would not carry
it.

### 3.4 Budget fixes before the low-rate profiles (P0)

The budget controller can only widen thresholds; three traffic sources sit outside it.

1. **Pose.** iOS sends `Pose` at a fixed 2 Hz (`ios/MinBand/Pipeline.swift`, `poseInterval`).
   At ~40 B payload plus 28 B header that is ~1 kbit/s on the wire by itself, more than the whole
   `telemetry` profile. Fix: derive the pose interval from the budget (e.g. 10 s, or off, below
   4 kbit/s). The frustum is cosmetic.
2. **Header.** The controller counts payload only (`core/src/edge.rs`, `emit` adds `b.len()` to
   `window_bytes`), while the link, the server metrics and `tools/eval` count +28 B per datagram.
   Fix: count the header in `window_bytes` so the controller targets what the link sees.
3. **Floor.** Keyframes every 2 s and the 5 s Hello refresh are not budgeted. The static scene's
   floor is 61.5 B/s on the wire (`docs/EVAL_FINDINGS.md`), about 490 bit/s, so `telemetry` at
   600 bit/s only fits small scenes until the compact codec (stretch S6) lands. Expect that and
   say it on stage.

After 1 and 2, rerun `cd core && cargo test` and `cd tools/eval && npm run eval`; regenerate the
golden file only if the change is intentional (`UPDATE_GOLDEN=1`).

### 3.5 Link box polish (stretch)

| # | Idea | Size |
|---|---|---|
| L1 | `tools/pi-link.sh <profile>` wrapping §3.2-3.3, plus `contested` as a background loop | S |
| L2 | The script also sets the edge budget and reports the profile name to the server so the viewer shows "LINK: lora 2 kbit/s" (needs a small `/api/link` endpoint) | S |
| L3 | Physical button on the Pi GPIO (gpiozero) that cycles profiles; an LED that goes dark on blackout | S |
| L4 | `simplex` profile: downlink 100 % loss, so the ground station never transmits (pairs with S1) | S |

## 4. Raspberry Pi 5 as an edge

No camera needed for the first two steps.

```bash
# Golden vectors on Linux aarch64: the same predictor, bit for bit, on companion-computer-class hardware.
curl --proto '=https' -sSf https://sh.rustup.rs | sh
cd core && cargo test

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
| V1 | **Video on the same link.** Beside the live twin, the halftone panel shows what video would deliver through the current profile: frames paint in line by line at the link rate (a ~30 KB still takes ~2 min at 2 kbit/s), with `NEXT FRAME 1:52`. Labelled as computed from the measured bitrate. | ~1/1000 of the bytes | `viewer/src/halftone.ts`, side-by-side panel | S-M |
| V2 | **Uncertainty rings in a blackout.** Each entity gets a hairline ground ring that widens with time since its last update; on reconnect the rings snap to points. Pairs with pulling the Pi's Ethernet cable. | Survives the link dying, honestly | `viewer/src/scene.ts` | S |
| V3 | **Packet waterfall and click.** A `LINK ACTIVITY` strip: time scrolls down, one tick per datagram, width = bytes (video would be a solid bar). Optional soft click per packet, like a Geiger counter: silent while predictable, clicks on turns. | Bandwidth proportional to surprise | structured per-datagram WS event (device, kind, bytes, ids) in `server/src/world.ts` (today it is only a text log line); viewer strip | M |
| V4 | **Tolerance bubble on the phone.** In the iOS `WIREFRAME` stage mode, a wire sphere of radius θ_pos x theta_scale sits on each person's ghost; walking stretches it, leaving it snaps it back and a packet goes. Needs an FFI accessor returning ghost positions predicted to `now` (ghosts are in `core/src/edge.rs`, `theta_scale` is already in stats). | The mechanism, without words | `core/src/edge.rs`, `core/src/ffi.rs`, `ios/MinBand/ARViewContainer.swift` | M |
| V5 | **Packets in the twin.** Using V3's event, each update draws a short line from the device frustum to the entity it corrects, with the error that triggered it (`+17 cm`). | Only surprises are sent | `viewer/src/scene.ts` | M |
| V6 | **Byte odometers.** `MINBAND 48 KB` vs `VIDEO 112 MB` since demo start, tabular digits, live ratio. | The headline number | viewer credits row | S |
| V7 | **Eight drones, one HF link.** Eight sim devices from the Pi through `hf`, each with a moving frustum over the terrain, fused into one picture. The sim does not send `Pose` yet. | Drones per link | `server/src/sim.ts` | S-M |
| V8 | **Live point on the eval curve.** The fidelity-vs-bytes chart with a dot that slides as the profile steps down. | Measured, not claimed | viewer, `runs/eval/fidelity_vs_bytes.csv` | M |

Props: a 128x64 monochrome OLED on the Pi showing profile and kbit/s plus a `JAM` toggle switch
(with L3); the phone on a pole or filmed from a mezzanine for a drone-like view; slides in the
same visual language, reusing the eval charts.

Order for the weekend: V1, V2, V3, then V4 (the iOS app is tested, so it is safe to build on).

## 6. Stretch ideas, in order

Pick from the top. Each is a vertical slice that can be demoed on its own.

| # | Idea | Where | Size | Why it scores |
|---|---|---|---|---|
| S1 | **Quiet / simplex mode.** Pre-provisioned edge that streams without waiting for an `Ack`, server option to never ack, `simplex` profile on the box. Today the edge sends only `Hello` until acked and the server acks up to 10x/s, so the ground station transmits about as often as the drone. | `core/src/edge.rs`, `ios/MinBand/Pipeline.swift`, `server/src/world.ts` | M | Ground transmitters get operators found; the design already tolerates no acks (keyframes) |
| S2 | **Airtime readout.** Datagrams/s and estimated time on air per profile next to bytes/s. | server metrics (`msgsPerSec` exists), viewer panel | S | Frames "less airtime" honestly |
| S3 | **Geodetic anchor.** Marker lat/lon/heading in config; server converts twin positions to WGS84 and MGRS; viewer shows the grid reference. | new `server/src/geo.ts`, viewer | S | No C2 system takes marker-frame metres; feeds P1 CoT |
| S4 | **Operator confirm / reject.** Click an entity in the viewer; state shown in the twin and carried into CoT. Server-side only. | `server/src/world.ts`, viewer | S | Human in the loop; decoys are common |
| S5 | **Military class set.** Add COCO car, truck, bus, motorcycle, bicycle, boat to the detector filter with a vehicle motion prior; label person as dismount. | `core/src/classes.rs`, `ios/MinBand/Detector.swift`, viewer | S-M | Judges map it to their own targets |
| S6 | **Compact codec tier.** Bit-packed fixed-point update (cm relative to a tile origin, quantised velocity), 8-16 B per entity, measured against postcard in `tools/eval`. | `core/src/wire.rs`, `tools/eval` | M-L | Needed for telemetry radios; "4-8x denser than compressed CoT on LoRa" becomes testable |
| S7 | **Compact authenticated encryption.** Pre-shared per-session key, 4-byte counter as implicit nonce, 64-bit tag (Ascon-AEAD128 or AES-GCM via a vetted crate, no custom crypto); overhead shown in the bytes graph. | new `core/src/crypto.rs` | M | First question from any military judge; ~12 B instead of 28 B per packet |
| S8 | **Image chip on demand.** Operator clicks an entity, the edge sends a 64x64 JPEG in budget-sized parts (`Hello.caps` bit1 is reserved for this). | core, iOS, server, viewer | L | Operator-verifiable evidence (the DARPA SA-ISR ask) |
| S9 | **Emission policy.** Modes silent / burst-on-event / minimum / normal, jittered keyframe period, batched sends, a max duty cycle beside the byte budget. | `core/src/edge.rs` | M | Removes the periodic 2 s / 5 s fingerprint |
| S10 | **MAVLink over serial, no radio needed.** Wrap datagrams in MAVLink 2 (e.g. the `TUNNEL` message, verify) over the Pi 5 UART to the laptop through a USB-serial adapter at 57600 baud. | new transport in server and a Pi bridge | M | Shows the path onto the low-bandwidth serial links drones already carry |
| S11 | **Pi 5 camera edge.** Camera Module 3, detector on the AI HAT+ (Hailo) or low-res CPU YOLO, positions from a flat-ground assumption (known camera height and tilt). A second real sensor for fusion. | new edge app on the Pi using the core natively | L | Edge AI on drone-class compute; the track's theme |
| S12 | **Real low-rate radio.** Pair of Meshtastic LoRa nodes (KR920 band in Korea) or a SiK telemetry pair; measured run replaces the emulated profiles. | `tools/` | L (hardware) | Measured beats emulated |
| S13 | **Utility test.** An operator reports count, class and location from the twin vs from video at the same budget, timed. | `tools/eval`, a script for the test | M | DARPA scores reduction "with preserved mission utility" |

## 7. After the hackathon

Geodesy with real error bars, global track IDs and a shared timebase, porting off ARKit (VIO,
rangefinder or terrain ray-cast), thermal, and the Ukraine and NATO pathways are in the report's
"Fielding" section and its months table. Next event on the list: EUDIS autumn hackathon,
15-17 Oct 2026.
