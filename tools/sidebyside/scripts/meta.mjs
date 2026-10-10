// Builds the /meta object from a footage run dir (shared by the mock server and the check scripts).
import fs from 'node:fs';
import path from 'node:path';

export function loadMeta(runDir) {
  const det = JSON.parse(fs.readFileSync(path.join(runDir, 'detect.json'), 'utf8'));
  const sum = JSON.parse(fs.readFileSync(path.join(runDir, 'summary.json'), 'utf8'));
  const g = sum.ground;
  // summary.camera_m = [-origin[0], h, origin[1]] (track.py): recover the ground-model origin.
  const origin = [-sum.camera_m[0], sum.camera_m[2]];
  return {
    fps: det.fps, width: det.width, height: det.height,
    start_frame: det.start_frame, end_frame: det.end_frame, every: det.every,
    ground: { f_px: g.f_px, pitch_deg: g.pitch_deg, height_m: g.height_m, hfov_deg: g.hfov_deg, cx: det.width / 2, cy: det.height / 2 },
    origin,
    camera_m: sum.camera_m,
    homographies: det.homographies,
  };
}
