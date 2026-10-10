# ClikaRT on-device AI — build plan (D4D x EDTH Seoul, SC1)

Status as of 2026-10-10 22:19 KST. Phase 0 and Phase 1 are done and verified. Phase 2
(Android) is in progress: AAR wired, license loader written, blocked on a pre-existing
gap unrelated to ClikaRT. Read "Session 2 log" at the bottom first — it corrects several
facts elsewhere in this doc (API shape, SDK floors) that were guesses, now confirmed.

## Context

MinBand streams bandwidth-minimal perception state over a single-digit-kbps link. For the
hackathon we add on-device AI on both ends of that link, running on CLIKA's ClikaRT runtime:

- **Edge 1 (airborne)** — Galaxy S26 Ultra, Snapdragon 8 Elite, ARM64. Detector on camera frames.
- **Edge 2 / hub** — Galaxy Book6 Pro, Intel Core Ultra X7 358H, x86_64. Same detector on drone
  footage, plus an LLM that turns the fused world state into a tactical briefing.
- **Transport** — NUCODE NU40DK boards over ~10 kbit/s BLE (`nucode/bridge.py`, built in parallel
  by a teammate; the directory is still empty).

The SC1 bonus requires the **exact same detector model** on both platforms, and the whole demo
must run with Wi-Fi off.

## Decisions already made (do not relitigate)

| Decision | Value | Why |
|---|---|---|
| Accelerator | **Vulkan Compute**, CPU fallback | ClikaRT has only CPU / CUDA / Vulkan / Metal backends |
| Intel NPU | **Dropped** from pipeline and from the pitch | No ClikaRT backend reaches it; using DirectML instead would break "built on ClikaRT" |
| Detector | **`ustc-community/dfine-nano-coco`** | In the catalog as family `d-fine`; COCO classes map straight onto `tools/footage/detect.py`'s existing `NAME_MAP` |
| SDK scope | Targeted bundles only | Full 2 GB archive (190 credits) is never downloaded |
| MCP | Not used | `clika-platform` MCP connects but its tools are absent from the running session; `clika-cli` covers everything |

## Phase 0 — environment (DONE, except the license)

Verified on this machine:

- Windows 11 26200, x86_64. Core Ultra X7 358H, 16 physical cores, 31.4 GB RAM. Arc B390.
- `.venv` on CPython 3.14.7 with `numpy 2.5.3`, `opencv-python 5.0.0.93`, `pyserial 3.5`,
  `wheel`, `pip 26.2.1`.
- ClikaRT **0.6.4** installed **twice on purpose**: into `.venv` (for `edge/` and `hub/` code) and
  into the global Python 3.14 so that `clikart-cli` and `clikart-license-init` sit on PATH via
  `%LOCALAPPDATA%\Programs\Python\Python314\Scripts`. Both share the same per-user license file,
  so a single `clikart-license-init` run licenses both.
- `clika-cli` 0.1.81 at `%LOCALAPPDATA%\Programs\clika\bin\`, authenticated, project
  `Default Project (04493906-6c74-4066-9a2d-1be4e87f1545)`.
- Wheel source of truth: `C:\Users\piotr\Desktop\Moje\D4D Seoul\clika-sdk\clika_runtime-0.6.4-cp314-cp314-win_amd64.whl`,
  sha256 `fa3a28963de3204bdf1c15ea736444c85c5b7337c9a8546caef5d1457dd9b61e`, 85.4 MB.

```
== compute devices on this machine ==
CPU      available
Vulkan   available
CPU:0     Intel                      31.37 GB  16 cores            fp16 yes  bf16 no  fp8 no  ok
Vulkan:0  Intel(R) Arc(TM) B390 GPU  17.88 GB  driver 101.8356     fp16 yes  bf16 no  fp8 no  ok
                                               Vulkan runtime 1.4.0, tensor_cores yes
```

Two hardware facts that constrain everything downstream:

1. The B390 is **integrated** — the runtime logs that it sizes its pool from system RAM. The
   17.88 GB is carved out of the same 31.4 GB the OS, the detector and the LLM all share. Budget
   the LLM accordingly; do not assume a private VRAM pool.
2. **`bf16 no`, `fp8_e4m3 no`** on both devices. Quantization choices for Phase 3 must land on
   fp16 or an integer scheme.

### 0.1 Remaining — license injection (you run this, not me)

Issue an **Offline** license on the platform (Licenses → Issue License): 2 hardware seats, expiry
≤ 17.10.2026, entitlements covering Windows x86_64 and Android arm64. The value is shown **once**
and starts with `CLIKA1-`.

Then, so the key never passes through a transcript or shell history:

```powershell
# prompts on stdin, echoes nothing but the path it wrote
clikart-license-init
# or, from a file you delete afterwards
clikart-license-init --from-file .\credential.txt
```

Writes `~/.clika/runtime/license`, owner-readable. `--force` is required to overwrite.
Until this is done every ClikaRT process logs:

```
[ClikaRT] [error] clikart license invalid — no ClikaRT license is configured for this process
```

`devices` and `list` still work without it; **inference does not**.

### 0.2 Remaining — phone

Plug in the S26 Ultra, accept the USB-debugging RSA prompt, confirm `adb devices -l` shows one
`device` line (not `unauthorized`). Blocks Phase 2 only.

### 0.3 Verification that Phase 0 is closed

```bash
clikart-cli devices                                   # Vulkan:0 Arc B390, status ok
clikart-cli info ustc-community/dfine-nano-coco       # verdict must no longer say "refused"
adb devices -l                                        # one authorized device
```

---

## Phase 1 — laptop detector (`edge/detector.py`)

### 1.1 Stage the model for offline use

Weights come from HuggingFace, **not** from CLIKA credits — fetching is free.

```bash
clikart-cli fetch ustc-community/dfine-nano-coco --cache-dir tools/footage/models/clika
# then prove the offline path works with the network still up:
clikart-cli --offline --cache-dir tools/footage/models/clika \
  ustc-community/dfine-nano-coco detect <some.jpg>
```

`tools/footage/models/` is already gitignored. The same snapshot directory is what gets pushed to
the phone in Phase 2 — that is literally how we satisfy the SC1 "identical model" rule.

### 1.2 First investigation of the session (do this before writing code)

Find out which surface `d-fine` exposes, because it decides the whole shape of `detector.py`:

```bash
clikart-cli ustc-community/dfine-nano-coco --help    # the model declares its own commands
```

We know it declares `detect`, `bench`, `serve`. The open question is whether the **Python** API
(`import clika_runtime`) gives us an equivalent pipeline object that returns decoded boxes, or
only a raw graph whose DETR-style output we must decode ourselves. Check
`clika_runtime/include/ClikaRT/AGENTS.md` (shipped inside the wheel) and the Python module's
`dir()` before guessing.

- **If a pipeline object exists** — use it. It post-processes for us.
- **If only a raw graph exists** — we decode D-FINE output ourselves: logits `(N, num_queries, 80)`
  and boxes `(N, num_queries, 4)` in normalized `cxcywh`; take top-k by sigmoid score, convert to
  pixel `xyxy`. D-FINE is DETR-lineage, so **per-tile NMS is not needed**, but cross-tile NMS
  still is, because overlapping tiles produce genuine duplicates.

### 1.3 Reuse, do not rewrite

`tools/footage/detect.py` already solves the hard parts and must be reused:

- `TiledDetector.tiles()` / `tile_for()` — overlapping tiling. A 4K drone frame shrunk to a 640 px
  network input loses people entirely; the tiling is why detection works at all on this footage.
  It also pads rather than stretches frames smaller than a tile, preserving magnification.
- `nms()` — class-wise NMS that passes `x, y, w, h` to `cv2.dnn.NMSBoxes`. The comment in the file
  explains why passing `x2, y2` silently suppresses people standing a few metres apart. Keep it.
- `KEEP`, `NAME_MAP`, `VISDRONE_TO_COCO`, `classmap_from_meta()` — model class names to MinBand
  ids: person 0, bicycle 1, car 2, motorcycle 3, bus 5, truck 7, armoured 101.

**The only ONNX-Runtime-specific code is two lines**: `ort.InferenceSession(..., providers=[...])`
in `__init__` and `self.sess.run(None, {self.inp: batch})` in `__call__`.

The minimal change is to let `TiledDetector` take an injected backend object exposing
`run(batch_nchw) -> ndarray`, defaulting to the existing ORT path. `edge/detector.py` then passes a
`ClikaBackend`. No new abstraction beyond that single seam — resist building a plugin registry.

### 1.4 Output contract

`edge/detector.py` writes **one JSON object per line to stdout**, so any transport can consume it
and we are not blocked on `nucode/bridge.py` existing:

```json
{"t": 1760100000123, "frame": 412, "w": 3840, "h": 2160,
 "dets": [[x1, y1, x2, y2, conf, cls], ...]}
```

`cls` is a MinBand id from `KEEP`. Pixel coordinates in the full frame, not tile-local.

CLI: `python edge/detector.py <video|dir> [--device vulkan|cpu] [--conf 0.25] [--tile N] [--max-fps N]`.

### 1.5 Footage

```bash
bash tools/footage/battlefield.sh      # populates tools/footage/clips/ (gitignored)
```

Ask before running — it pulls real drone footage and is a sizeable download.

### 1.6 Verification

- `python edge/detector.py tools/footage/clips/<clip>.mp4 --device vulkan | head -5` emits well
  formed JSON lines with non-empty `dets` on a frame that visibly contains vehicles or people.
- Same clip with `--device cpu` produces comparable detections. Any large divergence means the
  Vulkan path is wrong, not faster.
- `clikart-cli ustc-community/dfine-nano-coco bench` on both devices for the Phase 4 numbers.
- One `assert`-based self-check: feed a synthetic frame with a known box through the tiling and
  cross-tile merge, assert one box survives rather than N duplicates.

---

## Phase 2 — Android edge (S26 Ultra)

### 2.1 Download the AAR (needs explicit approval, ~14 credits)

```bash
clika-cli runtime-sdk download --platform android-arm64 --language kotlin \
  "C:\Users\piotr\Desktop\Moje\D4D Seoul\clika-sdk"
```

Must be the same **0.6.4** release as the desktop wheel. Verify the printed sha256.
The `android-arm64` archive (63.7 MB, backends `cpu, vulkan`) is the C++/CLI route; the Kotlin
Maven zip (~141.6 MB) is what the app module needs.

### 2.2 Wire it into the existing app

`android/app/build.gradle.kts` already has `compileSdk 36, minSdk 28` (CLIKA needs API 26+), already
declares `com.microsoft.onnxruntime:onnxruntime-android:1.20.0`, and already sets
`androidResources { noCompress += listOf("onnx") }`.

- Add `io.clika:clika-runtime:0.6.4` from the local Maven zip.
- Extend `noCompress` to cover the ClikaRT snapshot's weight file extension so the runtime can mmap
  it instead of inflating it onto the heap. Worth checking what extension the snapshot actually
  uses — the desktop wheel itself is LZMA-compressed throughout, so CLIKA is comfortable with
  compression formats that naive zip tooling rejects.
- Keep the ORT path behind a flag for one commit, so a failed Vulkan bring-up on Adreno does not
  leave the app with no detector at all.

### 2.3 License on Android

No home directory, so the per-user file does not apply:

```kotlin
class LicensedApp : Application() {
  override fun onCreate() {
    super.onCreate()
    ClikaRtAndroid.load(this, license = readCredential())   // from a secret store, never a literal
  }
}
```

An explicit value here beats the environment. Never commit the key; `readCredential()` reads from
local storage provisioned at install time.

### 2.4 Push the identical snapshot

```bash
adb push tools/footage/models/clika/<snapshot> /data/local/tmp/clikart/model
```

Same directory contents as the laptop. That identity is the SC1 bonus claim, so record the snapshot
hash on both sides for the Phase 4 report.

### 2.5 Verification

- `adb logcat` shows the ClikaRT Vulkan backend selecting the Adreno device, not falling back to CPU.
- The app produces detections on camera frames with the **same** model id and snapshot hash as the laptop.
- Airplane mode on, app still detects.

---

## Phase 3 — hub tactical briefing (`hub/briefing.py`)

### 3.1 Model

A ~3B instruct model from the catalog: `Qwen/Qwen2.5-3B-Instruct` or `meta-llama/Llama-3.2-3B-Instruct`.
Pick by measured prefill/decode on Vulkan:

```bash
clikart-cli fetch <model> --cache-dir tools/footage/models/clika
clikart-cli --offline <model> bench
```

Constraint from Phase 0: **no bf16, no fp8**. Use fp16 or an integer/GGUF quant (`--weights Q4_K_M`
for GGUF repos). On an integrated GPU sharing 31.4 GB with everything else, a 3B at Q4 is the safe
default; fp16 3B is ~6 GB of weights plus KV cache and will contend with the detector.

### 3.2 Input contract

Summarize the **fused** world state, not raw per-device entities. From `server/src/types.ts`:

- `GlobalEntity[]` — `gid`, `class`, `pos`, `vel`, `ce` (honest error radius in metres),
  `coasting`, `stale`, `geo` (`{lat, lon, mgrs}` when the geodetic anchor is configured).
- `LinkView` — `rateBps`, `airtimeShare`, `profiles`, and `contested` while that profile is active.
- `DeviceView.silent` / `lastSeenMs` — which edges have gone quiet.

The briefing is the place to make MinBand's honesty story visible: `ce` and `coasting` mean the
model should say "three dismounts, ~40 m uncertainty, coasting for 12 s" rather than inventing
precision the link never carried.

Read the snapshot from the server's WebSocket on :8080, the same feed the viewer consumes. For
development without a phone, `viewer/dev/mock-server.ts` already speaks the full snapshot contract
including the hackathon fields the server does not produce yet — use `npm run mock` in `viewer/`.

### 3.3 Output

A short MGRS-anchored brief: what is out there, where, how stale, link health, one line of
recommended action. Keep it under ~120 words — it is read aloud in a demo.

### 3.4 Verification

- `npm run mock` in `viewer/`, then `python hub/briefing.py` produces a brief naming entities that
  are actually in the mock scene.
- `MOCK_PHASE=blackout@8 MOCK_HOLD=1 npm run mock` — the brief must report entities as stale or
  coasting, not assert fresh positions.
- Wi-Fi off: the whole thing still runs.

---

## Phase 4 — performance and offline validation

Produce `reports/CLIKA_SC1.md` with:

- Device table from `clikart-cli devices` on both platforms.
- `clikart-cli bench` numbers for `dfine-nano-coco` on CPU:0, Vulkan:0 (Arc B390), and Adreno.
- End-to-end detector FPS from `edge/detector.py` at real footage resolution, with the tile count,
  since tiling multiplies the per-frame inference count.
- LLM prefill/decode tokens per second on Vulkan.
- The identical-model evidence: model id plus snapshot hash on both platforms.
- The offline evidence: commands run with Wi-Fi disabled, `--offline` in use, Offline license.
- An explicit note that the Intel NPU is present and unused, with the reason. Saying this plainly is
  stronger than letting a judge discover it.

---

## Risks still open

| Risk | Impact | When we find out |
|---|---|---|
| Python API may expose only a raw graph, forcing us to write D-FINE post-processing | Half a day of Phase 1 | First investigation, 1.2 |
| Vulkan on Adreno may fall back to CPU on the S26 Ultra | Phase 2 performance story collapses | First `adb logcat` run |
| `opencv-python 5.0.0.93` is a major-version jump; `cv2.dnn.NMSBoxes` signature may have moved | `detect.py` reuse breaks | First run of Phase 1 |
| Integrated GPU shares RAM with the OS; detector plus 3B LLM may contend | Hub throughput | Phase 3 bench |
| `nucode/bridge.py` does not exist yet | Nothing, if the detector stays on stdout JSON lines | Whenever the teammate lands it |
| Only 2 offline seats | A third device cannot join | Already known; do not register spares |

## Credits

5000 available, ~97 per GB. Spent: ~8 (the 85.4 MB wheel). The 159.3 MB `windows-amd64` archive was
**not** needed — the wheel ships `clikart-cli.exe`, `clikart-license-init.exe`, `ClikaRT_vulkan.dll`
and the full C++ headers. Planned: ~14 for the Android Kotlin AAR. Model weights come from
HuggingFace and cost nothing.

## Session 2 log (2026-10-10 21:30-22:19 KST) -- read this before trusting the phases above

### What is verified and committed (branch worktree-clika-detector, worktree at
.claude/worktrees/clika-detector; not yet merged to main)

- edge/detector.py plus edge/test_detector.py (commit 8832b90). Phase 1 done.
- android/settings.gradle.kts, android/app/build.gradle.kts dependency wiring
  (commit dcf6e36), then android/app/build.gradle.kts compileSdk/minSdk bump plus
  MinBandApplication.kt plus manifest registration (commit 82ae665).
- tools/footage/clips/mvt-test10.mp4 fetched (one clip, not the full battlefield
  set) and run through edge/detector.py on both Vulkan and CPU: 28 detections on
  frame 0, both backends, real vehicle/person boxes. Gitignored, not committed.
- tools/footage/models/clika/ holds the fetched ustc-community/dfine-nano-coco
  snapshot (14.6 MB, apache-2.0). Gitignored, not committed. Re-fetch command:
  clikart-cli fetch ustc-community/dfine-nano-coco --cache-dir tools/footage/models/clika
- C:\Users\piotr\Desktop\Moje\D4D Seoul\clika-sdk\ (sibling to the repo, not inside
  it): clika-runtime-maven-0.6.4.zip (141.6 MB, sha256 verified) unzipped to
  clika-sdk/maven/. Contains io.clika:clika-runtime-android:0.6.4 (the AAR) and
  two empty clika-runtime/clika-runtime-jvm POMs (ignore those, Android only).
- C:\Users\piotr\tools\jdk-21.0.12.1+1\ -- portable Temurin 21, no installer, extracted
  from the Adoptium API zip. Needed because the system JDK and Android Studio bundled
  JBR are both 25.0.3, and this Gradle/AGP throws IllegalArgumentException: 25.0.3
  trying to use it. android/gradle.properties now pins org.gradle.java.home to it.
- clika platform: both offline seats used. s26-ultra (phone, android/arm64) and
  PIOTREKBOOK (this laptop, windows/amd64), both online, agent 0.1.81. Do not
  generate more enrollment codes -- there is no third seat.

### Corrections to the phases above -- these were guesses before, now confirmed

1. Section 1.2 open question is answered: decoded pipeline, not raw graph.
   clika_runtime (top-level Python import) is pure tensor/graph plumbing with no
   detection decode. The decode lives in clika_runtime.modelverse
   (ModelRegistry.builtin().load_detection(source, LoadOptions(...)) ->
   DetectionModel, whose .detect(image_tensor_hwc3, confidence=, max_detections=)
   returns a Detections of already-decoded pixel-space Detection(xmin, ymin, xmax,
   ymax, score, label_id, label)). D-FINE query-set decode (sigmoid, box
   dequantize, top-k, the LQE head) runs inside ClikaRT_modelverse.dll/.so; Python
   never touches the raw (N, queries, 80/4) tensors. This collapses the planned
   manual-decode fallback entirely -- edge/detector.py's ClikaTiledDetector just
   calls .detect() per tile and offsets plus NMS the results, no sigmoid/cxcywh math.
   Tensor.from_data(ndarray) takes a plain C-contiguous numpy array directly (HWC
   uint8, RGB) -- no manual batching/NCHW needed, the model preprocesses internally.
2. Android SDK floors were wrong in section 2: compileSdk must be at least 36 and
   minSdk must be at least 28. Both come from the AAR itself -- the Gradle manifest
   merger and checkDebugAarMetadata refuse the build otherwise, with an explicit
   message naming the required version. Confirmed by actually building, not
   documentation.
3. The Android artifact id is clika-runtime-android, not clika-runtime.
   implementation("io.clika:clika-runtime-android:0.6.4"), resolved and verified
   via ./gradlew :app:dependencies --configuration debugRuntimeClasspath.
4. The Kotlin license API matches the plan guess, confirmed by decompiling
   classes.jar inside the AAR with javap: io.clika.runtime.ClikaRtAndroid.load(
   Context, String) (a load$default overload exists with the String optional,
   presumably falling back to an env var -- pass it explicitly anyway, never rely on
   env for a license). MinBandApplication.onCreate() now calls this, reading the
   key from filesDir/clika_license.txt (app-private storage, never committed,
   provisioned out-of-band via adb push plus run-as cp).
5. The d-fine checkpoint weight file is .safetensors, not LZMA-compressed as
   the desktop wheel AGENTS.md note led us to guess. noCompress in
   build.gradle.kts now covers both "onnx" and "safetensors".
6. cv2.dnn.NMSBoxes is unaffected by the opencv-python 5.0.0.93 major-version
   jump flagged as a risk in section 1 -- confirmed via the cross-tile-merge
   self-check and the real clip run; signature and behavior unchanged.

### The actual blocker right now

android/app/src/main/java/dev/minband/android/EdgeBridge.kt and Pipeline.kt import
dev.minband.core.FfiEdge / FfiTrack -- generated uniffi Kotlin bindings over a Rust
core, from the teammate's "android: Android edge app (ARCore + ONNX Runtime + uniffi
Kotlin core)" commit (3ea59bd/caf29fb). Those bindings are not present in this
worktree; tools/build-android.sh presumably generates them (needs a Rust toolchain:
cargo, uniffi-bindgen, likely cargo-ndk for the .so cross-compile). This has
nothing to do with ClikaRT -- it blocks :app:compileDebugKotlin for the whole app,
ClikaRT wiring or not. Confirmed by running ./gradlew.bat :app:compileDebugKotlin:
manifest merge and resource processing succeed (meaning the ClikaRT AAR, compileSdk 36,
minSdk 28 are all fine); only the Kotlin compile step fails, only on these two
preexisting files, not on MinBandApplication.kt.

### Next session: start here

1. Run tools/build-android.sh (read it first -- it is the teammate script, probably
   wraps cargo build --target aarch64-linux-android plus uniffi-bindgen plus copies the
   generated Kotlin into android/app/src/main/java/dev/minband/core/ and the .so
   into jniLibs/). This needs a Rust toolchain; check rustup show before assuming
   one is not installed. If a toolchain install is needed, stop and ask first (same
   guardrail as model/SDK downloads).
2. Once :app:compileDebugKotlin is clean, :app:assembleDebug, then
   adb -s 192.168.0.232:36731 install -r app-debug.apk (phone is probably still
   reachable at that address; adb connect again if not -- Wi-Fi only during dev,
   never during the Wi-Fi-off demo).
3. Provision the license on-device: adb push (credential file) /data/local/tmp/
   clika_license.txt, then adb shell run-as dev.minband.android cp /data/local/tmp/
   clika_license.txt files/clika_license.txt. Credential itself lives wherever
   clikart-license-init put it on the desktop (per-user, not in this repo) -- copy
   its value, do not regenerate a license.
4. Push the identical ustc-community/dfine-nano-coco snapshot to the phone (same
   snapshot dir as tools/footage/models/clika/, not a re-fetch) -- this identity is
   the SC1 bonus claim, so bitwise-same files, not just the same source string.
5. Wire edge/detector.py ClikaTiledDetector logic into Kotlin as the real
   Detector implementation (Detector.kt's interface already documents: a ClikaRT
   detector slots in here with the same contract), replacing or sitting alongside
   YoloOnnxDetector.
6. Only after the on-device detector runs: merge worktree-clika-detector into main
   (currently un-merged, pushed to origin as a branch) and consider Phase 2 closed.

### Prompt for next session

Read docs/CLIKA_PLAN.md in full, especially the Session 2 log at the bottom; it has
corrections to earlier sections (confirmed API shapes, SDK floors, artifact ids)
and the exact next-step list. You are resuming on branch worktree-clika-detector
(or merge main copy of it first). First task: get tools/build-android.sh running
so dev.minband.core (the uniffi Rust bindings EdgeBridge.kt and Pipeline.kt need)
exists, then get the app compiling, installed on the phone at 192.168.0.232:36731
(reconnect via adb connect if that is stale), and the ClikaRT license provisioned
on-device per the steps already written down. Stop and ask before installing a Rust
toolchain if one is not already present -- same rule as the model/SDK downloads.
