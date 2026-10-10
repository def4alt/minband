"""Live phone-camera source for MASt3R-SLAM, modeled on RealsenseDataset.

Accepted --dataset values:
  http://192.168.1.42:8080/video      IP Webcam (Android) MJPEG
  rtsp://192.168.1.42:8554/live       RTSP apps (e.g. "RTSP Camera Server", Larix)
  rtmp://... / udp://... / srt://...  anything FFmpeg can open
  phone:2  or  /dev/video2            virtual webcam (DroidCam, Iriun, v4l2loopback)

A background thread keeps only the newest frame, so SLAM always processes
the current view instead of draining a growing buffer (which causes lag).
"""

import atexit
import os
import pathlib
import threading
import time

import cv2
import numpy as np

from mast3r_slam.dataloader import Intrinsics, MonocularDataset
from mast3r_slam.mast3r_utils import resize_img

STREAM_PREFIXES = ("http://", "https://", "rtsp://", "rtmp://", "udp://", "srt://")


def is_phone_source(path):
    return path.startswith(STREAM_PREFIXES + ("phone:",)) or path.startswith("/dev/video")


def _open_capture(source):
    if source.startswith("phone:"):
        source = int(source.split(":", 1)[1] or 0)
    elif source.startswith("/dev/video"):
        source = int(source[len("/dev/video") :])
    if isinstance(source, int):
        cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    else:
        # Low-latency FFmpeg options for RTSP; harmless for http MJPEG.
        os.environ.setdefault(
            "OPENCV_FFMPEG_CAPTURE_OPTIONS",
            "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay",
        )
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


class PhoneCameraDataset(MonocularDataset):
    def __init__(self, source, max_width=640, timeout=10.0):
        super().__init__()
        # Live runs still save logs/phone_<time>.{ply,txt} + keyframes on quit
        # (close the viewer window), for the one-shot scene export.
        self.dataset_path = pathlib.Path(time.strftime("phone_%Y%m%d_%H%M%S"))
        self.source = source
        self.max_width = max_width
        self.timeout = timeout
        self.save_results = True
        # PHONE_STOP_ON_EOF=1: treat a dropped stream as the end (test runs
        # with a streamed file) instead of reconnecting forever.
        self.stop_on_eof = bool(os.environ.get("PHONE_STOP_ON_EOF"))
        self._ended = False
        if self.stop_on_eof:
            self.timeout = 3.0

        self.cap = _open_capture(source)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open phone stream: {source}")

        self._lock = threading.Lock()
        self._frame = None
        self._frame_time = 0.0
        self._frame_id = 0
        self._last_read_id = -1
        self._running = True
        self._thread = threading.Thread(target=self._grab_loop, daemon=True)
        self._thread.start()
        atexit.register(self.close)

        first, _ = self._wait_for_frame(-1)
        self._last_read_id = -1
        self.h, self.w = first.shape[:2]
        print(f"[phone] {source}: {self.w}x{self.h}")

        # Optional intrinsics, same as the RealSense path. main.py --calib
        # (config/intrinsics.yaml format) overrides this.
        if self.use_calibration and os.environ.get("PHONE_CALIB"):
            fx, fy, cx, cy = map(float, os.environ["PHONE_CALIB"].split(","))
            self.camera_intrinsics = Intrinsics.from_calib(
                self.img_size, self.w, self.h, [fx, fy, cx, cy]
            )

    def _grab_loop(self):
        fails = 0
        while self._running:
            ok, img = self.cap.read()
            if not ok or img is None:
                fails += 1
                if fails > 30 and self.stop_on_eof:
                    return
                if fails > 30:  # stream dropped (Wi-Fi hiccup): reconnect
                    print("[phone] stream lost, reconnecting...")
                    self.cap.release()
                    time.sleep(0.5)
                    self.cap = _open_capture(self.source)
                    fails = 0
                else:
                    time.sleep(0.01)
                continue
            fails = 0
            if self.max_width and img.shape[1] > self.max_width:
                s = self.max_width / img.shape[1]
                img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            with self._lock:
                self._frame = img
                self._frame_time = time.time()
                self._frame_id += 1

    def _wait_for_frame(self, last_id):
        t0 = time.time()
        while True:
            with self._lock:
                if self._frame is not None and self._frame_id != last_id:
                    self._last_read_id = self._frame_id
                    return self._frame, self._frame_time
            if time.time() - t0 > self.timeout:
                raise RuntimeError(f"No frame from {self.source} for {self.timeout}s")
            time.sleep(0.002)

    def __len__(self):
        # PHONE_MAX_FRAMES lets headless (--no-viz) runs stop and save.
        if self._ended:
            return len(self.timestamps)
        return int(os.environ.get("PHONE_MAX_FRAMES", 999999))

    def subsample(self, subsample):
        pass  # live stream: always use newest frame

    def get_img_shape(self):
        # Probe without consuming a frame/timestamp (keeps frame ids aligned).
        with self._lock:
            img = cv2.cvtColor(self._frame, cv2.COLOR_BGR2RGB)
        raw_img_shape = img.shape
        img = resize_img(img, self.img_size)
        return img["img"][0].shape[1:], raw_img_shape[:2]

    def get_timestamp(self, idx):
        return self.timestamps[-1]

    def read_img(self, idx):
        try:
            img, t = self._wait_for_frame(self._last_read_id)
        except RuntimeError:
            if not self.stop_on_eof or self._frame is None:
                raise
            print("[phone] stream ended")
            self._ended = True  # main loop stops at the next len() check
            img, t = self._frame, self._frame_time
        self.timestamps.append(t)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return img.astype(self.dtype)

    def close(self):
        if not self._running:
            return
        self._running = False
        self._thread.join(timeout=1)
        self.cap.release()
