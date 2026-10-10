# MinBand Android

The Android edge: the same pipeline as the iOS app (ARCore instead of ARKit, ONNX Runtime instead
of CoreML), the same Rust core through uniffi Kotlin bindings, the same wire protocol to the same
server. Kotlin, no Compose, no AppCompat; the UI follows `docs/STYLE.md` (monochrome on `#070809`,
hairlines, no icons). Portrait only, arm64 only.

## Build

```bash
tools/build-android.sh                 # from the repo root; rerun after any change in core/
cd android && ./gradlew :app:assembleDebug :app:testDebugUnitTest
./gradlew :app:installDebug            # phone with USB debugging (or wireless debugging) on
```

`tools/build-android.sh` cross-compiles `core/` with `--features ios` (the uniffi feature's name)
for `aarch64-linux-android`, using the NDK's clang as the linker (no cargo-ndk needed), and runs
the crate's own `uniffi-bindgen`. It writes (both gitignored):

- `app/src/main/jniLibs/arm64-v8a/libminband_core.so`
- `app/src/main/java/dev/minband/core/minband_core.kt`: the Kotlin bindings (`FfiEdge`,
  `FfiTrack`, `FfiEdgeStats`, `describe`, `cadence`), package from `core/uniffi.toml`.
  `EdgeBridge.kt` is the only caller. They need JNA (`net.java.dev.jna:jna@aar`).

Needs: `rustup target add aarch64-linux-android`, an NDK (`ANDROID_NDK_HOME`, else the newest
under `$ANDROID_HOME/ndk`), JDK 17+ (Android Studio's works), Android SDK 35. On a Windows GNU
Rust host the script also exposes the NDK's `llvm-dlltool` as `dlltool`, which the host-side
proc-macros need. Gradle: AGP 8.13, Kotlin 2.2, ARCore 1.49, ONNX Runtime 1.20.

## Detector model (gitignored)

The app looks for the first `.onnx` in `app/src/main/assets/`: an Ultralytics YOLO export
**without** NMS (`[1,3,S,S]` RGB in, `[1, 4+80, N]` out). The NMS and the class filter run in
`Detector.kt` (IoU 0.45, confidence 0.35, the 9 tracked COCO classes). Export YOLOv8n at 256 px (320 also works, ~1.3x slower but better on far/small objects):

```bash
python -m venv yolo-venv && yolo-venv/bin/pip install ultralytics onnx onnxslim
yolo-venv/bin/python -c 'from ultralytics import YOLO; print(YOLO("yolov8n.pt").export(format="onnx", imgsz=256, opset=17, simplify=True))'
cp yolov8n.onnx <repo>/android/app/src/main/assets/yolov8n-256.onnx
```

### GPU path (TFLite, optional)

`TfliteDetector` runs the same model on TFLite with the **GPU delegate** and is preferred over the
ONNX path when a `.tflite` is in assets. Convert the ONNX export to a **float32** TFLite with
`onnx2tf` (the GPU delegate runs it in FP16; a float16 model will not load on the CPU fallback) and
drop it next to the `.onnx`:

```bash
pip install onnx2tf tensorflow onnx-graphsurgeon sng4onnx onnxslim ai-edge-litert
onnx2tf -i yolov8n.onnx -o tflite_out -nuo
cp tflite_out/yolov8n_float32.tflite <repo>/android/app/src/main/assets/yolov8n-256.tflite
```

The delegate either runs the model on the GPU or, if it cannot apply (some SoCs reject the yolov8
head), falls back to TFLite CPU, then to the ONNX detector; the active backend is in the
`MinBand Detector: session` and timing log lines (`tflite-gpu`, `tflite-cpu`, `nnapi-fp16`,
`xnnpack`, `cpu`). **Measured on the test phone (MediaTek Dimensity 8200):** neither the TFLite GPU
delegate (OpenCL, "Error applying delegate") nor NNAPI (routes to the Neuron NPU, same error) can
take this yolov8 graph, so it runs on the CPU at ~200-300 ms either way; the GPU path helps only on
SoCs whose delegate accepts the model.

Without a model the app still runs (camera, origin, pose), the DETAILS line says "no detector",
and no tracks are produced. A ClikaRT detector implements the `Detector` interface in
`Detector.kt` (image + rotation in, boxes in captured-image coordinates out) and replaces
`YoloOnnxDetector` in `Pipeline`.

## Origin marker

The same printed marker as iOS (`ios/Markers/minband-marker-a3.png`, A3 landscape at 100 %,
**0.42 m** wide). It is copied into `app/src/main/assets/` and loaded into an ARCore
`AugmentedImageDatabase` at session start (downsampled 4x). Lay it flat on the floor. No marker:
**ORIGIN HERE** sets the origin at the camera position dropped onto the detected floor plane
(or 1.4 m below the camera), +Y up, +X the phone's right, -Z the viewing direction.

## Perception pipeline

```
ARCore Session.update (GL thread, 30 Hz) ──► every ~83 ms, one frame in flight ──► detect thread
  │                                YuvToRgb -> rotate upright -> 256x256 (letterbox) -> ONNX -> NMS
  │                                Lift3D: Depth API (5x5 median) | ray to the floor plane -> marker frame
  │                                Tracker.update: gated NN + constant-velocity Kalman
  ├─ 30 Hz: Tracker.tracks(at) -> GroundTruthLog -> EdgeBridge.tick -> UDP
  ├─ 2 Hz:  EdgeBridge.pose(camera.pose)  (once the origin is locked; the core decides which are sent)
  ├─ AugmentedImage (FULL_TRACKING) -> Origin.lock (re-lock only on >2 cm / >1 deg corrections)
  └─ Plane (horizontal, upward, below the camera) -> Origin floor height
```

| File | What |
|---|---|
| `Geometry.kt` | `Vec3`, `Vec2`, `Quat`, column-major `Mat4` (ARCore `Pose.toMatrix` layout). |
| `Models.kt` | `Detection`, `Track`, `WorldPoint`, `OverlayBox`, `TrackedClass` (ids from `core/src/classes.rs`). |
| `Origin.kt` | Marker frame, levelled to gravity; manual fallback; floor height. Same maths as iOS. |
| `Detector.kt` | `Detector` interface, `YoloOnnxDetector`, `YuvToRgb`. Boxes in captured-image (sensor) coordinates. |
| `Lift3D.kt` | Box -> 3D point in the marker frame, with the noise of that lift (`WorldPoint.sigma`) and the camera position (`eye`); `DepthSampler` over ARCore DEPTH16. People and chairs at their ground contact (ray to the floor), everything else by its width prior. |
| `Tracker.kt` | Pure Kotlin port of the iOS tracker; hits weighted by their own sigma, gate widened by the prediction's uncertainty, `Config.SLOW_DETECTOR` birth/death times (below). |
| `OverlayMotion.kt` | Moving / static per confirmed track for the "MOVING" label, the server's `movers.ts` rule in seconds. |
| `EdgeBridge.kt` | Kotlin face of the Rust core; bytes/s estimate; `Pose` encoding. |
| `Transport.kt` | `Transport` interface, `UdpTransport` (acks on the same socket). A NUCODE serial bridge goes here. |
| `GroundTruthLog.kt` | `gt-<unix>.csv` in `Android/data/dev.minband.android/files/` (tools/eval reads it); `DeviceIdentity`. |
| `DetectionLog.kt` | `det-<unix>.csv` next to it: every lifted detection with its sigma, the camera position and the track id it went to; `DetLogReplayTest` replays it. |
| `Pipeline.kt` | Threads, rates and wiring above; publishes `UiState` and the overlay to the Activity. |
| `BackgroundRenderer.kt` | Camera background (external OES texture) and the `GLSurfaceView` renderer that drives ARCore. |
| `OverlayView.kt`, `MainActivity.kt` | Corner-bracket boxes and `+` lift marks; HUD (LINK, TRACKS, ORIGIN), DETAILS, START, ORIGIN HERE, HOST. |

Differences from iOS, said plainly:

- **Depth.** ARCore's Depth API (depth from motion, or a ToF sensor where present) replaces
  LiDAR. If no depth image arrives within 10 s of the origin lock the session is reconfigured
  without depth (the Infinix X6880 advertises it but its depth-from-motion fails internally),
  and the lift uses the floor plane.
- **No-depth fallback** intersects the ray with the floor plane instead of ARKit's
  estimated-plane raycast (ARCore's hit test needs the live frame on the GL thread). The floor
  is the lowest upward-facing plane 0.8-2.5 m below the camera and at least 0.3 m^2 (a phantom
  plane 4 m down once sank every track), else the origin's own plane. A person whose feet are
  out of frame gets a distance from the box width and a 0.55 m shoulder prior, so people at
  arm's length are placed too (measured: tracks from 0.2 m to 7.8 m).
- **Origin.** START locks a manual origin after 3 s without a marker (ORIGIN HERE still re-sets
  it); a rehearsal that forgot the button produced no tracks.
- **Tracker at a slow detector.** The iOS numbers assume 12 Hz; the CPU detector here delivers
  1-4 Hz when hot. Three things had to change, each found on the device and reproduced in
  `PhoneReplayTest`: (1) `Tracker.Config.SLOW_DETECTOR`: birth after 2 hits, a tentative track lives
  1.5 s and a confirmed one 2 s, else a tentative track dies before its second hit and nothing is
  ever born (boxes lifted on every frame, zero tracks). (2) Each lift reports its noise
  (`WorldPoint.sigma`: ~0.1 m for depth or a ray to visible feet, ~20 % of the range for a person
  placed by the width prior) and the Kalman update weighs the hit by it; with a fixed 0.1 m the
  filter trusted width-prior hits so much that its velocity swung by jitter/dt. (3) The association
  gate follows the ray (`WorldPoint.eye`, the camera position at the hit): a lift is good across
  the ray (bearing) and poor along it (range: a person whose legs are hidden behind a desk or chair
  is placed 1.6-2.4x too far, because the ray to the box bottom meets the floor behind them; the
  session logs show the same person at 3.5 m and 5.5 m on consecutive hits). Across the ray the
  gate is the class gate plus walking speed times the time since the last hit; along it, 3 sigma of
  the prediction plus the hit's noise or half the range, whichever is larger; and a hit far beyond
  what the filter expects is weighed as an outlier (its sigma taken as innovation/3), so one bad
  range nudges a settled track instead of yanking it and its velocity. A wide isotropic gate was
  tried first: it kept ids but let a chair's hits land on the neighbouring chair's track, which then
  "flew" at its speed cap. In a 137 s room session the fixed gate made 112 ids for three people,
  each living 1.4-2.0 s, every new id restarting the MOVING history, so the tag never showed; on
  the clip with the phone's noise at 2.5 Hz: 87 ids, median life 2.4 s before, ~25 ids and ~6 s
  after. `tools/movers/gt_summary.py` prints this per track from a phone `gt-*.csv`, and
  `DetLogReplayTest` replays a phone `det-*.csv` (every lifted hit, with its sigma and camera
  position) through the tracker at the desk. The replay of a 36 s room session then showed the
  rest: ghosts. A track born from two hits 1 m apart (a cup's ray-to-floor lift flickering along
  the ray, a person behind a desk) took a 2.5 m/s velocity from the 1.5 m/s velocity prior and
  flew with it for the whole 2 s death time, and a static chair whose lift stepped 0.6 m was
  tagged MOVING. So: the velocity prior is per class (people 0.7 m/s, carried 0.3, placed 0.1),
  the state's speed is capped like the report, a track unseen for `coastS` (1 s) stops and
  reports zero velocity, the moving/static rule needs some speed behind a displacement
  (`SPEED_FLOOR` 0.2 m/s) and more displacement of placed (1 m) and carried (0.7 m) objects,
  and things that are not on the floor (cups, bottles, laptops, phones, bags, tvs) are placed by
  their width prior instead of a ray to the floor (`TrackedClass.widthPrior`, `onFloor`), chairs
  by the ray to the bottom of their box like a person's feet. On that session's hits: chairs and
  laptops 0 of 13 MOVING (were 4), people 11 of 13, the longest person track 27 s. Two sessions
  later the last false movers were standing people 5-6 m away whose range flipped by 0.5-0.8 m
  between hits, so the rule now judges a median position (1 s window, every 0.5 s), asks 2.5 lift
  sigma of displacement (0.9 m at 6 m; the floor sigma is the measured 6.5 % of range), needs the
  speed for 0.8 s, and waits for a track's third hit rather than one second of age (a new walker
  is tagged ~1 s after first sight instead of 1.5 s). The session after that tagged the two
  seated people instead: their boxes (1.1-1.8 widths tall, feet under the table) were placed by
  the width prior, which swung 0.9-7.7 m as the box took in arms or torso, and the two-hit birth
  velocity (0.7 m/s) lingered long enough for the speed-only rule. So the speed-only rule is gone
  (a mover must have moved, speed only backs the displacement), and a squat person box (under
  `SQUAT_ASPECT` = 2 widths tall: seated, or standing with the legs hidden) keeps its lift but
  with a sigma of 35 % of range, so the tracker barely moves it and the tag needs metres; a
  track's sigma is the median of its last five hits, so one squat frame of a walker changes
  nothing. The next session had every person box squat (a room of tables) and a walker among
  them was invisible to that rule, which is where the literature on monocular pedestrian
  localization points too: the range from a box is ill-posed under occlusion, the bearing is
  not ([MonoLoco](https://arxiv.org/abs/1906.06059), [GPENet](https://arxiv.org/abs/2211.01556)),
  and mobile pipelines judge motion in the image, compensated by the camera pose. So each lift
  now also carries its noise across the ray (`WorldPoint.sigmaAcross`, 3 % of range, 5 cm
  floor; measured 3-5 cm between a standing person's hits), the Kalman update splits the
  innovation into across and along the ray with separate gains and robust weights, and the
  MOVING rule (phone and server) asks the class distance across the ray or 2.5 sigma along it:
  a walker crossing the view behind the tables is tagged in ~1.5 s with a range that is
  guesswork; one walking straight at the phone with hidden legs still is not, and the demo
  should know it. People are also kept from 0.25 confidence (`TrackedClass.confidence`), since
  at 256 px a person 7 m away scored 0.25-0.35 and was seen every other second, too rarely to be
  born as a track. The first session with the bearing rule found two more things in the replay:
  the split must be on the ground plane only (a person's lifted height is their box centre while
  ARCore has no floor plane yet, and that height difference between a walker's standing and
  bending frames read as a bearing jump that broke their track), and people and chairs are
  dropped to y = 0 when no plane is known (the marker lies on the floor, a manual origin is
  dropped onto it). Placed and carried objects are judged across the ray only: a chair's range
  stepped a metre when the walker passed in front of it. The walker then reported the tag came
  and went: the detector had put a second box on them, which was born as a new id and took their
  next hits while the first id coasted away with the tag, and the new id started over from three
  hits. So two confirmed tracks of one class within 0.5 m merge (`Tracker.Config.mergeDist`), a
  new track born within 1 m of a mover alive or gone less than 2 s ago starts MOVING
  (`OverlayMotion.INHERIT_DIST`, `GHOST_S`, same class only; the server does the same), and a box
  the tracker has not matched yet shows the tag when its lifted point sits within 0.7 m of a mover
  (`Pipeline.NEAR_MOVER_M`). The run after that tagged the walker and the person seated beside
  them: ARCore's pose had run 26 m in 9 s with the walker filling the view (the camera position in
  `det-*.csv` tells), dragging everything in the marker frame with it. So the displacement judged
  is relative to the scene: when the tracks still being observed (hits coming) moved together the
  way the camera did, the camera's displacement is subtracted (a median of the tracks' own
  displacements was tried as the drift and made things worse: their along-ray noise became
  everyone's); and nothing is judged while the camera moves faster than 1 m/s (`EYE_MAX_SPEED`: a
  hand does not). On that session's tracks the walker keeps the tag and the seated neighbour
  loses it. The next run: a slow first pass (walking away, 0.2 m/s) was not tagged, a brisk second
  one was, and the neighbour was tagged again, by the along-ray rule this time (their width-prior
  range swung 1.2-2.2 m at 1.7 m). So the window is 3 s and a person needs 0.4 m (0.13 m/s is
  enough), the along-ray rule applies only when the range came from the feet or depth for three
  quarters of the window (`ALONG_SIGMA_REL`: lift sigma within 15 % of range; the server, without
  sigmas, asks 60 % of range) and changed one way (`ALONG_SLACK`: a flip goes and comes back, a
  walker approaching comes steadily closer), a classifier state survives a tick without its track
  (`KEEP_S`), and a new track inherits MOVING only from a mover the detector has lost (`LOST_S`),
  not from one still being hit beside it. On the phone's own tracks of that run the slow pass is
  tagged 2.5 s after it starts and the neighbour is not. The run after that (41 s: a walk 6 m
  away and back past the phone) tagged a standing person for a second as the walker passed them
  at 1.5 m on the same bearing: the walker's box filled 94 % of the frame, the detector's box
  ended at the knees short of the border, the floor ray at its bottom said 3.1 m with a feet
  ray's 0.1 m sigma, and the standing person's squat track (sigma 1.7 m, so an along-ray gate of
  metres) took the hit 1.4 m from them. Three things: `Lift3D` judges feet visibility on the
  box's edge (the feet point sits 3 % inside the box, which a frame-filling box puts past the
  margin) and refuses a floor ray for a person taller in pixels than anyone at that range
  (`TALL_RATIO`: the width prior instead); the tracker refuses a hit whose box is over twice as
  tall or short as the track's recent median, by more than 40 px (`Tracker.Config.sizeRatio`,
  `sizeSlackPx`: drone boxes are 15-35 px and flicker by that); and the tag on an unmatched box
  beside a mover needs the mover's class (a chair was tagged as the walker passed it). The same
  run had a chair MOVING for 3 s at its start while ARCore's VIO logged `Insufficient inliers`
  and the pose ran 1.2 m in 4 s with everything in the room following it (the replay's drag
  correction held the chair, the phone's borderline run did not): judgement now pauses while
  `Camera.trackingState` is not TRACKING, and every static -> moving flip is logged with its
  numbers (`flip:` in logcat, `OverlayMotion.flips`), so the next wrong tag is read off the log.
  Those lines paid off in the run after that (the holder walked at 0.3-0.6 m/s with the phone
  lowered, the walker came up to the lens): a track just born in a dragged scene was tagged with
  `raw=(0,0) drift=(0.48,-0.17)`, the first track's three seconds of camera drift subtracted from
  a newborn's nothing, so the drift subtracted is now the camera's displacement over each track's
  own window; the drag correction engaged only past 0.5 m of camera displacement while a person
  needs 0.4 m, so a slow drift tagged people (and session 16's chair, at 0.48 m) before it could
  help (`DRAG_MIN_M` 0.3, the server the same); judgement pauses above 0.5 m/s of camera speed
  (`EYE_MAX_SPEED`: a holder walking, not a hand); and a person box 90 % of the frame wide
  across gravity is not lifted at all (`Lift3D.FULL_WIDTH_FRACTION`: such lifts sat 0.01-0.4 m
  from the camera and moved with every step of the holder). A track that starts MOVING by
  inheritance is logged too.
- **Detector input** is letterboxed, not stretched, and ONNX Runtime runs on the CPU with
  NNAPI (GPU/NPU) when the device can run the graph there, else XNNPACK then plain CPU (`Detector.createWithProvider`, each a full session attempt so the detector never dies). On the Dimensity 8200 the GPU cannot run the whole yolov8 graph (ORT_EP_FAIL if forced GPU-only), so NNAPI partitions it back to the CPU and yolov8n-256 takes ~110-160 ms cool, ~230-300 ms throttled hot (3-6 Hz); no net GPU gain there. YUV conversion ~5 ms.
- **Camera** picks the widest GPU texture ARCore offers (1920x1080) with a 640x480 CPU image;
  the aspect-fill preview on a 20:9 screen still shows ~80 % of the width.
- **No WIREFRAME stage mode, no H.264 baseline recording.**
- **Byte counter.** `LINK` shows the measured wire rate with 28 B UDP/IP per datagram over a 2 s
  window, like iOS. On the venue Wi-Fi a 30 s run lost 3 of 85 datagrams (3.5 %), repaired by
  the protocol; `adb logcat -s "MinBand Wire"` prints every datagram and ack.

## Tests

```bash
cd android && ./gradlew :app:testDebugUnitTest
```

JVM tests, no device: tracker birth/death/ids/gating/clamping and a constant-velocity target within
10 %, origin conversions (flat, tilted, wall, manual, quaternion), unprojection, gravity-down and
bottom point, ray-to-floor, depth median, the box rotation mapping, host parsing, the GT CSV row,
the on-screen moving/static rule (`OverlayMotionTest`).

`PhoneReplayTest` replays the whole tracking chain on a real clip: the phone's own detector output
(`tools/movers/phone_detect.py` runs the `yolov8n-256.onnx` asset on `tools/footage/clips/vtest.avi`,
OpenCV's pedestrian clip, and writes `app/src/test/resources/replay/vtest-yolov8n-256.csv`) through
`Lift3D.lift`, `Tracker` and `OverlayMotion` at 30 Hz, at detector rates from 10 Hz down to the
1.25 Hz of a hot phone, with and without the width-prior lift's jitter. It checks that every walker
keeps one id and gets the MOVING tag at every rate. Re-run the detector step after changing the
model or the decode; the test itself needs no Python.

The same test also replays **drone footage**: `tools/movers/footage_boxes.py` turns a
`tools/footage` run (MEVA UAV clip, tiled detection every 6th frame, the registration's
homographies and the ground fit's camera) into `replay/drone-meva.csv`, and
`droneClipWalkersAreMovers` runs the chain on it with the lift's range cull lifted to 150 m
(`Lift3D.lift(maxRange)`; the phone keeps 8 m). The clip is unlabelled, so the bounds are sanity
ones against `track.py track`'s own numbers for the run. Making this work changed the lift's
noise model for everyone: a floor ray's sigma is now range^2 / (camera height x focal length) x
8 px (the phone at 5 m: 0.3 m, as measured; the drone at 50 m: 0.3 m too), across the ray
range / focal x 3 px, instead of fixed fractions of range that only fit the phone.

`DetLogReplayTest` does the same with a phone session's own hits: pull its `det-<unix>.csv`, set
`MINBAND_DET_LOG` to the path and run the test with `-i`; it prints one line per id (class, life,
displacement, top speed, MOVING) and totals, so a tracker change is judged on the exact hits that
misbehaved. Without the variable it is skipped.

### Needs a device

Everything ARCore: marker detection and its axis convention (check `ORIGIN locked` and the `+`
marks landing on feet), depth sampling, the overlay alignment (`transformCoordinates2d`),
detector latency (YOLOv8n 256 on ONNX Runtime, NNAPI with CPU fallback; the active provider is in the `MinBand Detector: session` log line and each timing line), end-to-end bytes/s
on the server (`curl localhost:8080/api/metrics`, the device shows up with its `deviceId`).

## Run

1. Server on the laptop: `cd server && npm run start`. Phone and laptop on the same Wi-Fi.
2. Install the app, allow the camera. If Google Play Services for AR is missing the app asks to
   install it.
3. **HOST**: `laptop-ip:7777` (the laptop's Wi-Fi IPv4; `ipconfig` / `ifconfig`). Remembered.
4. **START**. LINK reads `waiting` until the server acks, then kbps. Point at the marker (or press
   **ORIGIN HERE**), ORIGIN reads `locked`, tracks appear in the viewer.
5. **DETAILS** (top right) shows SEQ, θ, budget, FPS, detector Hz, wire and core B/s, depth mode,
   model, device id, and confidences in the labels.
6. Ground-truth logs: `adb shell ls /sdcard/Android/data/dev.minband.android/files/` and
   `adb pull /sdcard/Android/data/dev.minband.android/files/gt-<unix>.csv runs/phone/`.
