# Handoff: continue MinBand on a local machine

Status 2026-10-10, branch `claude/dazzling-carson-mlx4xb` (judging 11 Oct). What exists, what is
left and where it goes: the team showcase (private link, share it from its Share menu) and
`docs/HACKATHON_PLAN.md`. Findings: `docs/EVAL_FINDINGS.md` (synthetic, links),
`docs/FOOTAGE_FINDINGS.md` (real drone footage, battlefield detection).

## Rules that hold everywhere

- Work on `claude/dazzling-carson-mlx4xb`; no pull request unless the team asks for one.
- Never commit model weights (`aerial-guardian.onnx` is AGPL) or any frame of the battlefield
  clips (several have no licence; numbers only, in docs and slides). `runs/` is gitignored.
- Scope: situational awareness with a human in the loop, affiliation unknown (`a-u-G`). No
  targeting, aim points, engagement or fire control. Pins (S24) are for awareness only.
- Battlefield held-out protocol: tune on `dev` clips only; run `heldout` once, with frozen
  parameters (`tools/footage/battlefield-clips.json`).

## Set up

```bash
git clone https://github.com/def4alt/minband && cd minband && git checkout claude/dazzling-carson-mlx4xb
mise install                                   # rust + wasm targets, wasm-pack (or install them yourself)
# Node 22, Python 3.10+, ffmpeg; binaryen 117+ for wasm-opt (older distro builds break wasm-bindgen)
cd core && wasm-pack build --target nodejs --out-dir pkg-node --release -- --features wasm
wasm-pack build --target web --out-dir pkg-web --release -- --features wasm && cd ..
for d in server viewer tools/eval e2e; do (cd $d && npm install); done
(cd e2e && npx playwright install chromium)    # the cloud box had it preinstalled
```

Rebuild both WASM packages after any change in `core/` (a stale `pkg-node` once made cars cost a
delta per frame).

## Check that it works

```bash
(cd core && cargo test)                        # 45 unit + 2 golden
(cd server && npm test)                        # 86+
(cd tools/eval && npm test)                    # 11
bash tools/test/pi-link.test.sh                # 80 dry-run checks
(cd e2e && npm test)                           # 28 end-to-end; the tc tests need Linux and root, they skip otherwise
```

Demo without a phone: `cd server && npm run dev`, `npm run sim`, `cd viewer && npm run dev`, open
`http://localhost:5173/?stage=1`.

## Real drone footage (MEVA) and the battlefield clips

```bash
cd tools/footage
python3 -m venv .venv && .venv/bin/pip install onnxruntime opencv-python-headless numpy scipy
mkdir -p models && curl -fsSL -o models/yolo11n.onnx https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx
curl -fsSL -o models/aerial-guardian.onnx https://raw.githubusercontent.com/Halok600/The-Aerial-Guardian/main/web/model/aerial-guardian.onnx   # AGPL, VisDrone-trained; never commit
.venv/bin/python meva.py fetch 2018-03-13.16-00-14 clips/   # 270 MB by byte range
./battlefield.sh clips/battlefield                          # 8 clips, about 120 MB, sha256-checked (tested)
./run_clip.sh clips/battlefield/mvt-test10.mp4 ../../runs/footage/mvt-test10   # one clip, the frozen pipeline end to end
```

`models/`, `clips/`, `.venv/` and `*.onnx` are gitignored. The full battlefield reproduction (all
clips, audit, tables) is "Reproducing" in `docs/FOOTAGE_FINDINGS.md`; the pipeline is described in
`tools/footage/README.md`. Results land in `runs/footage/<clip>/`. 4K detection is CPU-bound, about 3-4.5 s per detection
frame on 4 cores: a 90 s clip at 5 Hz takes about an hour, so run it in the background, without a
time limit. With a GPU, `pip install onnxruntime-gpu` instead of `onnxruntime`.

Live replay of the tracks through the real server and viewer:
`cd server && TRACKS=../runs/footage/<run>/tracks.csv TRACKS_CAMERA=<camera_m from summary.json> npm run sim`.

## What is left, in order

Before judging (people and hardware):
1. Slide 1: track statement, a mentor's real scenario and link rates, team names. Share the deck.
2. iPhone app: build on a Mac (new H.264 measurement code is not compiled yet), run it for 60 s,
   copy the file to `runs/baseline_a.json`.
3. Pi 5 link box: `sudo tools/test/pi-link-kernel.test.sh` with real netem on the Pi, then phone,
   Pi and laptop through every profile.
4. Record the fallback run on the final build (`cd e2e && npm run record`), rehearse both
   versions, pull the cable on clean or HF.

Engineering:
5. Battlefield detection, done on the held-out clips (pooled track recall 0.25 -> 0.48 at equal
   precision; military clips 0.13 -> 0.38). Left, in "Unfinished" of `docs/FOOTAGE_FINDINGS.md`:
   the MEVA 4K re-detection with the fixed detector (killed at 88 % by a time limit; about 45 min)
   and the visual audit on MEVA 4K. Known weak spots: thermal recall, roof vents boxed as vehicles,
   shadows and surf as false movers. No military appearance model is used (the one tried labelled
   cars and portable toilets as armour), so nothing emits class 101 yet.
6. Tracker quality on real footage is the largest cost: 273 tracks for about 80 objects, 183
   births a minute, parked objects wander about 0.4 m. Fewer, longer tracks cut the link cost.
7. Stretch, in the plan's order: S24 video first with pins (new), S6 compact codec, S1 quiet mode,
   S8 chips on demand, priority under budget.
