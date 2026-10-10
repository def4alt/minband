"""Simulated phone camera for tests: serves an MJPEG stream over HTTP like the Android "IP Webcam" app.
Holds the clip's first frame frozen until FLAG_FILE exists (SLAM finished loading and is processing frames),
then plays the clip at its native frame rate and closes the stream (so PHONE_STOP_ON_EOF=1 ends SLAM).
Never blocks forever: accept() times out after ACCEPT_S, the frozen pre-roll is capped at PREROLL_S (then the clip plays anyway).
usage: python sim_phone_server.py CLIP.mp4 PORT FLAG_FILE"""
import sys, time, os, socket, cv2
clip, port, flag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
ACCEPT_S = float(os.environ.get("SIM_ACCEPT_S", 300)); PREROLL_S = float(os.environ.get("SIM_PREROLL_S", 900))
cap = cv2.VideoCapture(clip); fps = cap.get(cv2.CAP_PROP_FPS) or 30.0; ok, first = cap.read()
assert ok, "cannot read clip"
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); srv.bind(("127.0.0.1", port)); srv.listen(1); srv.settimeout(ACCEPT_S)
print(f"[sim-phone] serving {clip} on http://127.0.0.1:{port}/video, waiting for a client (<= {ACCEPT_S:.0f} s)", flush=True)
try: conn, _ = srv.accept()
except socket.timeout: print(f"[sim-phone] no client within {ACCEPT_S:.0f} s, exiting", flush=True); sys.exit(0)
conn.settimeout(60)  # SLAM's grabber may stall while the model loads
def send(img):
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85]); b = jpg.tobytes()
    conn.sendall(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(b) + b + b"\r\n")
t = time.time(); n = 0
try:
    conn.recv(4096)
    conn.sendall(b"HTTP/1.0 200 OK\r\nConnection: close\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n")
    while not os.path.exists(flag) and time.time() - t < PREROLL_S:  # frozen pre-roll at 10 fps until SLAM is ready
        send(first); n += 1; time.sleep(0.1)
    print(f"[sim-phone] {'flag seen' if os.path.exists(flag) else 'pre-roll cap reached'} after {time.time()-t:.1f} s of frozen pre-roll ({n} frames); playing clip at {fps:.0f} fps", flush=True)
    t0 = time.time(); i = 0
    while True:
        ok, img = cap.read()
        if not ok: break
        send(img); i += 1
        d = t0 + i / fps - time.time()
        if d > 0: time.sleep(d)
    print(f"[sim-phone] clip done ({i} frames, {time.time()-t0:.1f} s)", flush=True)
except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError) as e:
    print(f"[sim-phone] client gone: {e}", flush=True)
conn.close()
