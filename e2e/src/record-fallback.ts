// Records the fallback run for the presentation: the real server, sim edges and the viewer in
// headless Chromium, stepped through the link profiles of the Pi box (docs/HACKATHON_PLAN.md 3.3)
// and a blackout, captured as video. Output: runs/fallback/fallback-<stamp>.webm (+ .mp4 when
// ffmpeg is on PATH) and a cue sheet with what happened when. The viewer is in STAGE mode (link
// activity strip, video on this link) unless STAGE=0.
//   npm run record                         # default script, ~2.5 min
//   DEVICES=2 npm run record               # fusion demo
//   STAGE=0 npm run record                 # operator view instead of stage mode
//   TRACKS=<tracks.csv> TRACKS_CAMERA=x,y,z FAKE_CAMERA=<clip.mjpeg> npm run record
//                                          # real drone footage (tools/footage): the sim replays the
//                                          # track log and the viewer's video panel shows the clip
//                                          # through Chromium's fake camera (not frame-synced);
//                                          # BASELINE_A=<baseline_a.json> uses the H.264 measured on it
import { mkdirSync, renameSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { chromium } from 'playwright';
import { ROOT, startStack, startViewer, getJson, sleep } from '../lib/stack.ts';

const OUT = join(ROOT, 'runs', 'fallback');
const SIZE = { width: 1920, height: 1080 };
const DEVICES = process.env.DEVICES ?? '1';

/** [seconds to hold, label, action] */
const STEPS: [number, string, (api: string) => Promise<unknown>][] = [
  [20, 'clean Wi-Fi: walkers in straight lines cost almost nothing, turns spike', async api => getJson(`${api}/api/link?profile=clean`)],
  // The blackout comes first, on the 2 s heartbeat: after a slow profile the receiver keeps the
  // slow thresholds for one old coast period (18.8 s after telemetry), so coasting would show late.
  [16, 'blackout 10 s: entities coast, rings grow, then stale', async api => getJson(`${api}/api/shaper?enabled=1&loss=1&revertAfterMs=10000`)],
  [12, 'link back: re-sync within one keyframe', async () => undefined],
  [20, 'hf 9.6 kbit/s, 500 ms', async api => getJson(`${api}/api/link?profile=hf`)],
  [25, 'lora 2 kbit/s, 10 % loss: the fidelity knob widens thresholds', async api => getJson(`${api}/api/link?profile=lora`)],
  [25, 'telemetry 600 bit/s: keyframe period stretches with the budget', async api => getJson(`${api}/api/link?profile=telemetry`)],
  [20, 'contested: lora with random 1-5 s blackouts', async api => getJson(`${api}/api/link?profile=contested`)],
  [8, 'clean again', async api => getJson(`${api}/api/link?profile=clean`)],
];

const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
mkdirSync(OUT, { recursive: true });
const replay: Record<string, string> = process.env.TRACKS ? { TRACKS: process.env.TRACKS, TRACKS_CAMERA: process.env.TRACKS_CAMERA ?? '0,40,40' } : {};
// BASELINE_A: a measured H.264 table for the footage (tools/footage/h264.sh), for the video panel.
const server: Record<string, string> = process.env.BASELINE_A ? { MINBAND_BASELINE_A: process.env.BASELINE_A } : {};
const s = await startStack({ server, sim: { DEVICES, ...replay } });
const viewer = await startViewer(s.server);
const camera = process.env.FAKE_CAMERA
  ? ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', `--use-file-for-fake-video-capture=${process.env.FAKE_CAMERA}`]
  : [];
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', ...camera] });
const ctx = await browser.newContext({ viewport: SIZE, recordVideo: { dir: OUT, size: SIZE }, ...(camera.length ? { permissions: ['camera'] } : {}) });
const page = await ctx.newPage();
const cues: string[] = [];
try {
  await page.goto(viewer.url + (process.env.STAGE === '0' ? '' : '?stage=1'));
  await page.waitForFunction(() => document.getElementById('status')?.hidden, null, { timeout: 20_000 });
  await sleep(4000);
  const t0 = Date.now();
  for (const [hold, label, act] of STEPS) {
    cues.push(`${((Date.now() - t0) / 1000).toFixed(1).padStart(6)} s  ${label}`);
    console.log(cues[cues.length - 1]);
    await act(s.server.api);
    await sleep(hold * 1000);
  }
} finally {
  const video = page.video();
  await ctx.close(); await browser.close(); await viewer.stop(); await s.stop();
  if (video) {
    const webm = join(OUT, `fallback-${stamp}.webm`);
    renameSync(await video.path(), webm);
    writeFileSync(join(OUT, `fallback-${stamp}.cues.txt`), cues.join('\n') + '\n');
    const mp4 = webm.replace(/\.webm$/, '.mp4');
    const r = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', webm, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '23', '-movflags', '+faststart', mp4]);
    console.log(r.status === 0 ? `wrote ${mp4}` : `wrote ${webm} (no mp4: ${r.error?.message ?? r.stderr?.toString().trim()})`);
  }
}
process.exit(0);
