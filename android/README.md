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
`Detector.kt` (IoU 0.45, confidence 0.35, the 9 tracked COCO classes). Export YOLOv8n at 320 px:

```bash
python -m venv yolo-venv && yolo-venv/bin/pip install ultralytics onnx onnxslim
yolo-venv/bin/python -c 'from ultralytics import YOLO; print(YOLO("yolov8n.pt").export(format="onnx", imgsz=320, opset=17, simplify=True))'
cp yolov8n.onnx <repo>/android/app/src/main/assets/yolov8n-320.onnx
```

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
  │                                YuvToRgb -> rotate upright -> 320x320 (scaleFill) -> ONNX -> NMS
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
| `Lift3D.kt` | Box -> 3D point in the marker frame; `DepthSampler` over ARCore DEPTH16. People at their feet. |
| `Tracker.kt` | Pure Kotlin port of the iOS tracker, same constants. |
| `EdgeBridge.kt` | Kotlin face of the Rust core; bytes/s estimate; `Pose` encoding. |
| `Transport.kt` | `Transport` interface, `UdpTransport` (acks on the same socket). A NUCODE serial bridge goes here. |
| `GroundTruthLog.kt` | `gt-<unix>.csv` in `Android/data/dev.minband.android/files/` (tools/eval reads it); `DeviceIdentity`. |
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
- **Detector input** is letterboxed, not stretched, and ONNX Runtime runs on the CPU with
  XNNPACK: yolov8n-320 takes 90-150 ms on the Dimensity 8200 (6-10 Hz), YUV conversion 5 ms.
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
bottom point, ray-to-floor, depth median, the box rotation mapping, host parsing, the GT CSV row.

### Needs a device

Everything ARCore: marker detection and its axis convention (check `ORIGIN locked` and the `+`
marks landing on feet), depth sampling, the overlay alignment (`transformCoordinates2d`),
detector latency (YOLOv8n 320 on ONNX Runtime CPU/NNAPI, expect 20-60 ms), end-to-end bytes/s
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
