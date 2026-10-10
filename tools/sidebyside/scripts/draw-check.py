#!/usr/bin/env python3
"""Paint the projected tracks of runs/sidebyside/check-points.json on the matching frame of the
720p file (what the page shows) -> runs/sidebyside/check-overlay.jpg. Dots must sit on the cars
and people. Run with tools/footage/.venv/bin/python (needs cv2)."""
import json, os, subprocess, sys
import cv2, numpy as np

repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
pts = json.load(open(os.path.join(repo, 'runs/sidebyside/check-points.json')))
video = sys.argv[1] if len(sys.argv) > 1 else os.path.join(repo, 'runs/sidebyside/meva-720p.mp4')
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(repo, 'runs/sidebyside/check-overlay.jpg')
t = pts['clipT']
raw = subprocess.run(['/opt/homebrew/bin/ffmpeg', '-v', 'error', '-ss', f'{t:.4f}', '-i', video, '-frames:v', '1',
                      '-f', 'image2pipe', '-vcodec', 'png', '-'], check=True, capture_output=True).stdout
img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
H, W = img.shape[:2]
sx, sy = W / pts['width'], H / pts['height']
col = {0: (0, 255, 255), 100: (200, 200, 200), 101: (0, 0, 255)}
for p in pts['points']:
    u, v = int(round(p['u'] * sx)), int(round(p['v'] * sy))
    if not (0 <= u < W and 0 <= v < H): continue
    c = col.get(p['cls'], (255, 128, 0))
    cv2.circle(img, (u, v), 7, c, 2)
    cv2.putText(img, str(p['id']), (u + 8, v - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, c, 1, cv2.LINE_AA)
cv2.putText(img, f"tick {pts['tick']} clipT {t:.3f}s  {len(pts['points'])} tracks (yellow=dismount, blue=vehicle, grey=mover)",
            (10, H - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
cv2.imwrite(out, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
print('wrote', out, f'{W}x{H}')
