# Edge hardware survey: what a battlefield drone (and a phone) can actually sense

Status: 2026-10-10, research for the feature-extraction pivot (send relevant events, not every
update). Web sources at the bottom; figures marked *recall* are from general knowledge and should
be checked against a datasheet before they go on a slide.

## 1. Three drone tiers you meet at the front

| Tier | Airframe | Flight stack | Sensors on board | Camera | Data link (down) | Companion compute |
|---|---|---|---|---|---|---|
| A. FPV / custom quad | 7-10" quads, Ukrainian "Mavic-class" clones | Betaflight, INAV, ArduPilot | IMU (gyro+accel) always; barometer usually; GPS + magnetometer optional external modules | Analog 5.8 GHz or digital (DJI O3, Walksnail, HDZero); cheap thermal 256x192 or 640x512 | ELRS / Crossfire 900 MHz control link with a telemetry back-channel of ~80 bit/s to ~1.2 kbit/s (ELRS 1:2 ratio in MAVLink mode ≈ 125 B/s); increasingly fibre-optic (full bandwidth, 10-20 km tether) | Raspberry Pi 4/5, Orange Pi 5 (RK3588, 4 GB), LTE modem over Ethernet (Spiderweb); Jetson not confirmed in the field |
| B. Closed quad | DJI Mavic 3 / 3E / 3T, Autel EVO Max 4T | DJI/Autel closed | GPS, IMU, baro, compass, 3-axis gimbal with reported pitch/roll/yaw (NED yaw), obstacle sensors; **laser rangefinder** on 3E/3T (3-1200 m, ±(0.2 m + 0.15 % D) *recall*) | Wide 84° + tele 15° FOV, 56x hybrid zoom; 3T thermal 640x512 @ 30 Hz | O3 Enterprise: 1080p video 8-15 km, falls to nothing under jamming | **The phone on the controller.** DJI MSDK v5 exposes aircraft location, attitude, gimbal attitude, zoom, and the rangefinder's distance and target lat/lon to an app on the phone |
| C. Fixed-wing recon | Leleka-100, Shark, Furia | Vendor autopilot, autonomous when jammed | GPS/INS, baro, 2-3-axis EO/IR gimbal; Shark has a laser rangefinder that gives target coordinates directly | 30x optical zoom EO, thermal | Encrypted FHSS link, 45-80 km, 720p/1080p video + telemetry; relays via another UAV | Vendor ground station; targets go to Delta by hand or vendor integration |

What is common to all three: an IMU, a barometric altitude, a camera, and some form of heading.
GPS exists on B and C and on most of A, but is the first thing jamming or spoofing takes away.
A rangefinder exists only on B (Enterprise) and C.

## 2. Phone (iPhone) as the edge

- Sensors: wide/ultrawide/tele cameras, IMU, barometer, magnetometer, GNSS (dual-frequency L1/L5 on
  iPhone 14 Pro and later *recall*), UWB, LiDAR on Pro models (range ~5 m: useless from altitude,
  fine for an indoor demo).
- Compute: CoreML YOLO at 10-15 Hz on the NPU, VideoToolbox H.264/HEVC, ARKit visual-inertial
  odometry (drifts, and iOS 26.4 users report a LiDAR-device drift regression).
- Where the phone really sits in the field: on the controller (tier B), where it has both the video
  feed and the drone's telemetry through the SDK. On tier A it is at best the pilot's second screen.
  So "phone as edge" is realistic for tier B and as a stand-in for a companion computer on A.

## 3. Links the events have to fit through

| Link | Rate | Note |
|---|---|---|
| ELRS telemetry, standard ratios | 78 bit/s (1:128) to ~1.2 kbit/s (1:16, burst) | Shares the control link; survives longer than video |
| ELRS MAVLink mode | ~1 kbit/s (125 B/s at 50 Hz 1:2) | "Stubborn sender" retries, so goodput drops under interference |
| SiK 57600 telemetry radio | ~3.5-4 kB/s used by default MAVLink streams | A 3-message low-bandwidth set is ~300 B/s |
| LoRa / Meshtastic | 0.3-27 kbit/s | Order of magnitude; the hackathon's `lora` profile is 2 kbit/s |
| Fibre | tens of Mbit/s | Not our problem |
| LTE (opportunistic) | bursts, then nothing | Store and forward |

The existing MinBand profiles (hf 9.6 kbit/s, lora 2 kbit/s, telemetry 600 bit/s) bracket this.

## 4. Who consumes the data in Ukraine

- **Delta** (MoD situational awareness): ingests 75,000 video streams a day; its Vezha module runs AI
  detection on streams (about 12,000 targets a week claimed); objects are stored as typed records
  with first-seen time, strike count, who struck; targets are auto-assigned to strike drones.
  Integrations listed: Virazh, Kropyva, Hrafit, ATAK.
- **Kropyva**: Android app, target coordinates to the nearest artillery battery; positions of allied
  units; short texts.
- **ATAK/TAK**: Cursor on Target XML. Point = lat, lon, hae, ce, le; detail/track = course, speed;
  type = a-u-G... (unknown affiliation, ground); `how` = m-p/m-f etc. MinBand already exports this.

The decision these systems support, in order of frequency: "is there something new here" (count and
type), "is it moving and where to" (course/speed), "exactly where" (10 m is enough for Delta, artillery
wants better), "is it still there".

## 5. A format worth stealing from: STANAG 4607 GMTI

Every dwell (one look of the sensor) carries a header with the sensor's position, orientation and
uncertainties plus a 64-bit existence mask saying which optional fields are present. Each target
report in the dwell is then tiny: delta latitude and delta longitude (16 bit each) relative to the
dwell centre, height 16, radial velocity 16, SNR 8, class 8, class probability 8, and optional
uncertainties. That is the same shape as "sensor pose once, targets as small deltas" and it is what
NATO fusion systems already understand.

## 6. Geolocation error without a rangefinder (first-order, flat ground)

At 100 m AGL, 45° depression, consumer GPS/IMU: altitude error ~4 m, 1° gimbal pitch ~3.5 m, 1°
heading ~1.7 m, drone horizontal GPS 2-3 m, combined ~5-8 m. Attitude error is the dominant term;
at 20° depression the same 1° pitch is ~15 m. With a rangefinder the range term disappears but the
attitude term stays. Field anecdote (ArduPilot terrain lookup): within ~10 m at 100-200 m.
Consequence: a centimetre threshold means nothing in the field; the position bucket is ~5-10 m and
the honest `ce` must carry that.

## Sources

- https://www.expresslrs.org/info/telem-bandwidth/ , https://www.expresslrs.org/software/mavlink/
- https://betaflight.com/docs/wiki/guides/current/Supported-Sensors
- https://ardupilot.org/copter/docs/common-mavlink-configuration.html , https://discuss.ardupilot.org/t/lowering-telemetry-datarates-in-ardupilot/82830
- https://dronexl.co/2025/06/30/ukraines-sky-squad-dji-mavic-3-autel-drones/ , https://azov.one/en/blog/drones/drone-mavik-3-thermal
- https://www.csis.org/analysis/how-ukraines-spider-web-operation-redefines-asymmetric-warfare , https://militarnyi.com/en/news/ukrainian-military-analyze-russian-drone-equipped-with-mv-tech/
- https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Key_Gimbal_GimbalKey.html
- https://ukrspecsystems.com/drones/leleka-100-electric-uav , https://www.globalsecurity.org/military/world/ukraine/shark-uav.htm , https://www.pravda.com.ua/eng/news/2025/05/05/7510715/
- https://en.wikipedia.org/wiki/Delta_(situational_awareness_system) , https://cepa.org/article/the-heart-of-war-ukraines-key-battlefield-system/ , https://ukrainesarmsmonitor.substack.com/p/combat-software-in-the-service-of
- https://www.wireshark.org/docs/dfref/s/s4607.html , https://www.mitre.org/news-insights/publication/evolution-standard-stanag-4607-nato-gmti-format
- https://www.mintlify.com/FreeTAKTeam/FreeTakServer/api/cot/message-format
- https://discuss.ardupilot.org/t/can-we-use-lua-script-to-locate-geolocation-of-ground-object-position-coordinate/102180 , https://www.mdpi.com/1424-8220/22/5/1903
- https://www.newgeopolitics.org/2025/12/28/ukraines-deftech-at-the-end-of-2025-from-drone-mass-to-systems-warfare/ , https://nextgendefense.com/ukraine-drones-vision-navigation/
