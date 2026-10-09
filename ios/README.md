# MinBand iOS

## Build

```bash
tools/build-ios.sh                    # from the repo root; rerun after any change in core/
cd ios && mise exec -- xcodegen generate && open MinBand.xcodeproj
# or headless:
xcodebuild -project MinBand.xcodeproj -scheme MinBand \
  -destination 'generic/platform=iOS Simulator' -configuration Debug build CODE_SIGNING_ALLOWED=NO
```

`tools/build-ios.sh` (uses the mise toolchain) cross-compiles `core/` with `--features ios` for
`aarch64-apple-ios` and `aarch64-apple-ios-sim`, runs the crate's own `uniffi-bindgen` and writes:

- `Frameworks/MinBandCore.xcframework`: `libminband_core.a` per slice plus the C module
  `minband_coreFFI` (`Headers/minband_coreFFI/module.modulemap`). Linked, not embedded.
- `MinBand/Generated/minband_core.swift`: the Swift bindings (`FfiEdge`, `FfiTrack`,
  `FfiEdgeStats`, `describe(bytes:)`), compiled into the app. `EdgeBridge.swift` is the only
  caller.

Both are gitignored, so a fresh checkout needs the script before Xcode can build. There is no
bridging header. The simulator slice is arm64 only, so `project.yml` excludes x86_64 for the
simulator. `PROFILE=debug tools/build-ios.sh` gives an unoptimised core.

## Detector model (gitignored)

The app looks for a CoreML object detector that ends in an NMS stage (Vision must return
`VNRecognizedObjectObservation`). Anything in `MinBand/Models/` is picked up by `xcodegen` and
compiled by Xcode into `MinBand.app/<name>.mlmodelc`; `Detector` also accepts an uncompiled
`.mlpackage`/`.mlmodel` copied into a `Models/` folder of the bundle and compiles it once at
runtime. Without a model the app still runs (AR preview, origin, pose), the HUD says
"no detector", and no tracks are produced.

YOLOv8n at 320 px with NMS, as used for the M4 build (ultralytics 8.4.174, torch 2.8.0,
coremltools 9.0, macOS system Python 3.9; the venv ends up ~750 MB, and the export downloads
the 6 MB `yolov8n.pt`; about 2 minutes in total):

```bash
python3 -m venv /tmp/yolo-venv
/tmp/yolo-venv/bin/pip install ultralytics coremltools
cd /tmp && /tmp/yolo-venv/bin/python -c \
  'from ultralytics import YOLO; print(YOLO("yolov8n.pt").export(format="coreml", imgsz=320, nms=True))'
cp -R /tmp/yolov8n.mlpackage <repo>/ios/MinBand/Models/
cd <repo>/ios && mise exec -- xcodegen generate     # the model must exist at generate time
```

The exported pipeline has three inputs (`image`, `iouThreshold`, `confidenceThreshold`, none
optional); `Detector` feeds the thresholds (IoU 0.45, confidence 0.35) through
`VNCoreMLModel.featureProvider`. YOLOv11n (`yolo11n.pt`) exports the same way.

## Origin marker

- Asset: `MinBand/Assets.xcassets/Markers.arresourcegroup/minband-marker-a.arreferenceimage`,
  AR resource group `Markers`, physical width **0.42 m** (420 x 294 mm).
- Print `ios/Markers/minband-marker-a3.png` (4961 x 3473 px, 300 dpi) on A3 landscape at
  **100 %**, no "fit to page". The printed image must be 420 mm wide; most printers cannot print
  borderless A3, so print borderless, or on larger paper and trim, or measure the printed width
  and put it (in metres) into that `Contents.json` as `"width"`. A 1 % width error scales every
  distance by 1 %.
- Lay it flat on the floor, matte paper, no glare. Marker frame: origin at the image centre,
  +X to the right along the width (reading the label), +Z toward the label edge, +Y up.
- Regenerate (deterministic, Pillow): `python3 ios/Markers/generate_marker.py`. It also writes
  the app icon.
- No marker on stage: press **origin: set here**; the origin becomes the camera position dropped
  onto the detected floor plane (or 1.4 m below the camera), +Y up, +X the phone's right, -Z the
  viewing direction. Seeing the marker later replaces the manual origin.

## Perception pipeline

```
ARSession (60 Hz, arQueue) ──► every ~83 ms, one frame in flight ──► detectQueue
  │                                Detector: Vision + CoreML, portrait (.right), scaleFill
  │                                Lift3D: LiDAR depth (5x5 median) | plane raycast -> marker frame
  │                                Tracker.update: gated NN + constant-velocity Kalman
  ├─ 30 Hz: Tracker.tracks(at: frame time) -> GroundTruthLog -> EdgeBridge.tick -> UDP
  ├─ 2 Hz:  EdgeBridge.pose(ARCamera.transform)  (only once the origin is locked)
  ├─ ARImageAnchor -> Origin.lock (re-lock only on >2 cm / >1 deg corrections)
  └─ ARPlaneAnchor (horizontal, below the camera) -> Origin floor height
```

| File | What |
|---|---|
| `Origin.swift` | Marker frame. `lock(markerTransform:)` takes `ARImageAnchor.transform` and levels it to gravity (Y = world up, X = image width projected horizontal, Z = X x Y); `toMarker`, `toMarkerDirection`, `toMarker(transform:)`, `rotationToMarker()` (world -> marker, used by `EdgeBridge.pose`), `lockManual(cameraTransform:)`, floor height. Thread-safe. |
| `Detector.swift` | Loads the first model in the bundle, runs `VNCoreMLRequest` on `capturedImage` with orientation `.right`, keeps the 9 tracked COCO classes (`person 0, backpack 24, handbag 26, bottle 39, cup 41, chair 56, tv 62, laptop 63, cell phone 67`, same ids as `core/src/classes.rs`) at confidence >= 0.35. Boxes come back in normalized **captured-image** coordinates (sensor landscape, origin top-left), the space ARKit's intrinsics, raycast queries and `displayTransform` use. |
| `Lift3D.swift` | Box -> 3D point in the marker frame. Depth: 5x5 median at the box centre, `.low` confidence ignored, unprojected with `ARCamera.intrinsics` scaled to `imageResolution`. No depth: `ARSession.raycast` (`.estimatedPlane`, horizontal) from the centre, or for people from the gravity-bottom of the box. People are reported at their feet (the viewer draws capsules above `pos`). No depth and no hit: dropped. |
| `Tracker.swift` | Pure Swift. Greedy global nearest neighbour gated per class (0.7 m person, 0.4 m otherwise), CV Kalman per track (6D state, one shared 2x2 covariance since the axes are identical and independent), process noise q = 0.1 m^2/s^3 (0.02 for chair/laptop/tv), measurement sigma 0.1 m. Birth after 3 hits (tentative tracks die after 0.35 s), death after 1.0 s without hits, ids monotonically increasing and never reused. `tracks(at:)` extrapolates with velocity clamped to the class max speed (3 m/s, 1 m/s for chair/laptop/tv, as in the core). Confidence is an EMA with a 12/255 hysteresis so the core's conf buckets do not flap. |
| `Pipeline.swift` | Threads, rates and wiring above. Publishes `originLocked`, `originSource`, `trackCount`, `bytesPerSec` (core estimate), `wireBytesPerSec` (measured, +28 B UDP/IP per datagram), `seq`, `thetaScale`, `detections`, `fps`, `detectHz`, `detectorStatus`, `depthMode`, `status`. Before the origin is locked the edge is ticked with `[]` (Hello only) and no Pose is sent. |
| `GroundTruthLog.swift` | `Documents/gt-<unix>.csv`, `tick,id,class,x,y,z,vx,vy,vz,conf` (tools/eval reads it), 5 decimals, buffered and written once per second and on stop. Visible in the Files app; **share log** opens a share sheet for the newest file. |
| `ContentView.swift`, `ARViewContainer.swift` | Camera view with RGB axes at the origin (X red, Y green, Z blue), detection boxes labelled `class #trackId conf` (thick = feeding a confirmed track), status line, stats line, host field (`host` or `host:port`), Start/Stop, origin: set here, share log. Portrait only. |

Tuning was done in simulation (`MinBandTests/TrackerTests`): at 12 Hz and 0.1 m noise,
q = 0.1 gives ~0.1 m/s per-axis velocity noise (below the core's 0.3 m/s `theta_vel`) and
follows a 90 degree turn at 1 m/s within ~0.8 s with < 0.25 m lag.

### Tests

```bash
cd ios && mise exec -- xcodegen generate
xcodebuild test -project MinBand.xcodeproj -scheme MinBand \
  -destination 'platform=iOS Simulator,name=iPhone 17'
```

`MinBandTests` is an unhosted logic-test bundle that compiles the perception files directly
(no core, no app launch): tracker birth/death/ids/gating/clamping and a constant-velocity target
within 10 %, origin conversions (flat, tilted, wall, manual, quaternion), label mapping and
Vision-to-captured-image rect mapping, unprojection, GT CSV format, and, if the model was present
at generate time, a real Vision pass. A developer-only end-to-end orientation check runs YOLO
on a photo both upright (`.up`) and as the sensor would deliver it in portrait (`.right`):

```bash
python3 -c "from PIL import Image; im=Image.open('bus.jpg'); im.save('/tmp/up.png'); im.rotate(90, expand=True).save('/tmp/raw.png')"
TEST_RUNNER_MINBAND_TEST_IMAGE=/tmp/up.png TEST_RUNNER_MINBAND_TEST_IMAGE_RAW=/tmp/raw.png \
  xcodebuild test ... -only-testing:MinBandTests/DetectorTests
```

### Needs a device

The simulator has no ARKit world tracking, so these are untested until run on a phone: marker
detection and the image-anchor axis convention (check the RGB axes on the marker), LiDAR depth
sampling and plane raycasts, `displayTransform` overlay alignment, detector latency on the
Neural Engine (expected ~15 ms for YOLOv8n 320 on A15+), and end-to-end bytes/s.

Ownership: `Detector`, `Lift3D`, `Tracker`, `Origin`, `Pipeline` and the UI are the perception
side; `EdgeBridge` and `Generated/` belong to the core bridge.
