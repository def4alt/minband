// The tracker's image boxes per track, from a footage run's detlog.npy (tools/footage/track.py):
// columns frame, source, class, conf, x1, y1, x2, y2, track id, ... Returns a lookup
// boxAt(trackId, tick) -> the track's last box at or before `tick`, normalised (centre u, v, w, h),
// which is what the edge's tracker knows about where the object is in the image.
import fs from 'node:fs';
import path from 'node:path';

function readNpy(file) {
  const buf = fs.readFileSync(file);
  // Magic (6 bytes), major and minor version, then the header length: u16 in v1, u32 in v2+.
  const v1 = buf[6] === 1;
  const at = v1 ? 10 : 12, hlen = v1 ? buf.readUInt16LE(8) : buf.readUInt32LE(8);
  const start = at + hlen;
  const header = buf.subarray(at, start).toString('latin1');
  if (!header.includes("'<f8'") || header.includes('True')) throw new Error(`${file}: expected little-endian float64, C order`);
  const shape = /'shape': \((\d+), (\d+)\)/.exec(header).slice(1).map(Number);
  const data = new Float64Array(buf.buffer.slice(buf.byteOffset + start, buf.byteOffset + start + shape[0] * shape[1] * 8));
  return { rows: shape[0], cols: shape[1], at: (r, c) => data[r * shape[1] + c] };
}

export function loadBoxes(runDir, tickHz = 120) {
  const file = path.join(runDir, 'detlog.npy');
  if (!fs.existsSync(file)) return () => null;
  const det = JSON.parse(fs.readFileSync(path.join(runDir, 'detect.json'), 'utf8'));
  const a = readNpy(file);
  const by = new Map();
  for (let r = 0; r < a.rows; r++) {
    const tid = a.at(r, 8); if (!(tid > 0)) continue;
    const tick = Math.round(((a.at(r, 0) - det.start_frame) / det.fps) * tickHz);
    const x1 = a.at(r, 4), y1 = a.at(r, 5), x2 = a.at(r, 6), y2 = a.at(r, 7);
    const box = [(x1 + x2) / 2 / det.width, (y1 + y2) / 2 / det.height, (x2 - x1) / det.width, (y2 - y1) / det.height];
    let l = by.get(tid); if (!l) by.set(tid, (l = [])); l.push([tick, box]);
  }
  for (const l of by.values()) l.sort((p, q) => p[0] - q[0]);
  return (id, tick) => {
    const l = by.get(id); if (!l || l[0][0] > tick) return null;
    let lo = 0, hi = l.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (l[m][0] <= tick) lo = m; else hi = m - 1; }
    return l[lo][1];
  };
}
