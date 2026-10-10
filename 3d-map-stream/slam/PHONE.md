# MASt3R-SLAM on the 5090 (home) + phone camera

    conda activate mast3r-slam        # has CUDA 12.8 nvcc, torch 2.11+cu128, TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1
    cd ~/MASt3R-SLAM

Live phone (same Wi-Fi):
    python main.py --dataset http://PHONE_IP:8080/video --config config/base.yaml   # Android "IP Webcam"
    python main.py --dataset rtsp://PHONE_IP:8554/live  --config config/base.yaml   # RTSP apps
    python main.py --dataset phone:2 --config config/base.yaml                       # virtual webcam /dev/video2 (DroidCam/Iriun)
Close the viewer window to stop -> logs/phone_<time>.ply (+ .txt trajectory, keyframes/).
Headless: add --no-viz and PHONE_MAX_FRAMES=300 so it stops and saves.
Calibrated: --calib config/intrinsics.yaml (width/height must match the frames, which are downscaled to 640 wide).

Fallback (recorded clip):
    python main.py --dataset clip.mp4 --config config/base.yaml

Re-install: ./install_5090.sh (idempotent). Local patches for 5090/torch 2.11:
  setup.py (+sm_120 gencode), backend gn_kernels.cu (linalg_norm -> norm),
  matching_kernels.cu + curope kernels.cu (.type() -> .scalar_type()), gcc-13 host compiler, cython<3, opencv<4.12/numpy<2.
