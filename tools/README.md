# tools

- `link.sh` - macOS dummynet shaping of UDP :7777 (`sudo tools/link.sh set 8Kbit/s 150ms 0.05`).
- `build-ios.sh` - cross-compile the core for iOS and build the XCFramework.
- `eval/` (M7) - replay ground-truth logs from the phone through the WASM Edge with swept
  thresholds; emit `fidelity_vs_bytes.csv` and the resilience table. Baseline A (H.264) is
  measured on the phone with VideoToolbox from the same recording; Baseline B is computed from the
  log as `entities * 31 B * 30 Hz`.
