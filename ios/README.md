# MinBand iOS

```bash
cd ios && xcodegen generate && open MinBand.xcodeproj
```

- Add a `Markers` AR Resource Group in `Assets.xcassets` with the printed origin marker
  (A3, high contrast; set its physical width in Xcode, it matters for scale).
- Drop a CoreML YOLO (e.g. `yolov8n.mlpackage`, 320 px) into `MinBand/Models/` (gitignored).
- `tools/build-ios.sh` builds `Frameworks/MinBandCore.xcframework` and the uniffi Swift
  bindings into `MinBand/Generated/`.

Ownership: `Detector`, `Lift3D`, `Tracker`, `EdgeBridge` are independent files; see
`Stubs.swift`. The `Pipeline` wiring is fixed.
