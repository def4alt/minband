# Real drone footage

MinBand on footage from a real drone instead of the phone: detect and track the objects the way a
drone edge would, then feed the tracks to the same WASM edge, eval and live server as a phone log.

Footage: [MEVA](https://mevadata.org) UAV drop 1 (Kitware / IARPA, **CC-BY-4.0**): 45 clips from
two DJI Inspire 1 v2 drones with Zenmuse X3 cameras, 3840x2160 at 30 fps, over the Muscatatuck
Urban Training Center (school, bus station, parking), re-encoded by the publisher at CRF 26. No
annotations ship with the UAV clips, so the tracks come from a detector and tracker, as on a real
edge.

## Pipeline

```bash
python3 -m venv .venv && .venv/bin/pip install onnxruntime opencv-python-headless numpy scipy
.venv/bin/python meva.py fetch 2018-03-13.16-00-14 clips/          # 270 MB by range request (meva.py index lists all)
curl -sSLO https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx   # or a VisDrone-trained ONNX
.venv/bin/python track.py detect clips/2018-03-13.16-00-14.16-03-38.uav1.mp4 --model yolo11n.onnx \
    --start 15 --end 105 --out ../../runs/footage/meva-uav-2018-03-13.16-00-14   # slow: ~3 s per 4K frame on 4 cores
.venv/bin/python track.py track ../../runs/footage/meva-uav-2018-03-13.16-00-14    # seconds: ground fit, tracker, tracks.csv
.venv/bin/python track.py preview ../../runs/footage/meva-uav-2018-03-13.16-00-14 clips/<clip>.mp4
./h264.sh clips/<clip>.mp4 15 105 ../../runs/footage/meva-uav-2018-03-13.16-00-14   # x264 on the same pixels
cd ../eval && npm run eval -- --gt ../../runs/footage/<run>/tracks.csv \
    --baseline-a ../../runs/footage/<run>/baseline_a.json --out ../../runs/footage/<run>/eval
cd ../../server && TRACKS=../runs/footage/<run>/tracks.csv TRACKS_CAMERA=<summary.json camera_m> npm run sim
```

- `detect.py`: a YOLO ONNX model on overlapping tiles at native resolution (people are 15-35 px
  tall in 4K from 60 m), class-wise NMS; COCO models and VisDrone-trained ones (classes mapped to
  COCO ids, which are MinBand's) both work.
- `track.py detect`: every 6th frame (5 Hz; the phone's detector runs at ~12 Hz, its tracker at
  30 Hz), the detector plus an ORB/RANSAC homography onto the first frame, so the drone's own drift
  and yaw do not become object motion.
- `track.py track`: a flat-ground pinhole camera with the focal length from the field of view
  (Zenmuse X3 16:9 video: ~85 deg horizontal), its pitch and height fitted from people's box widths
  (1/range is linear in the image row), detections at the bottom centre of their boxes (feet,
  tyres), a constant-velocity Kalman tracker in metres (two-stage association, birth after 3 hits,
  1 s coasting), the state written at every frame. `summary.json` reports the fit and two checks:
  walkers' median speed (~1.3-1.4 m/s expected) and the extent of the scene.
- `h264.sh`: libx264 veryfast, zerolatency, 2 s keyframes, no B-frames, CRF 23 and 28, at native,
  720p, 480p and 360p; `baseline_a.json` (CRF 23) replaces the configured H.264 rows in `tools/eval`
  and the server (`MINBAND_BASELINE_A`).

Limits, said plainly: the metric scale rests on an assumed person width (0.55 m) and a flat
ground; the detector is a nano model, so dense groups are under-counted and tracks fragment; the
source was already re-encoded at CRF 26, so x264 on it is a lower bound for camera-original video.
