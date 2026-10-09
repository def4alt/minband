// Records the fallback run for the presentation: the real server, sim edges and the viewer in
// headless Chromium, stepped through the link profiles of the Pi box (docs/HACKATHON_PLAN.md 3.3)
// and a blackout, captured as video. Output: runs/fallback/fallback-<stamp>.webm (+ .mp4 when
// ffmpeg is on PATH) and a cue sheet with what happened when. The viewer is in STAGE mode (link
// activity strip, video on this link) unless STAGE=0.
//   npm run record                         # default script, ~2.5 min
//   DEVICES=2 npm run record               # fusion demo
//   STAGE=0 npm run record                 # operator view instead of stage mode
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
  [20, 'hf 9.6 kbit/s, 500 ms', async api => getJson(`${api}/api/link?profile=hf`)],
  [25, 'lora 2 kbit/s, 10 % loss: the fidelity knob widens thresholds', async api => getJson(`${api}/api/link?profile=lora`)],
  [25, 'telemetry 600 bit/s: keyframe period stretches with the budget', async api => getJson(`${api}/api/link?profile=telemetry`)],
  // Back to a 2 s heartbeat first: on telemetry coasting starts only after ~19 s of silence.
  [10, 'clean again: the 2 s heartbeat returns', async api => getJson(`${api}/api/link?profile=clean`)],
  [16, 'blackout 10 s: entities coast, rings grow, then stale', async api => getJson(`${api}/api/shaper?enabled=1&loss=1&revertAfterMs=10000`)],
  [15, 'link back: re-sync within one keyframe', async () => undefined],
  [20, 'contested: lora with random 1-5 s blackouts', async api => getJson(`${api}/api/link?profile=contested`)],
  [8, 'clean again', async api => getJson(`${api}/api/link?profile=clean`)],
];

const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
mkdirSync(OUT, { recursive: true });
const s = await startStack({ sim: { DEVICES } });
const viewer = await startViewer(s.server);
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
const ctx = await browser.newContext({ viewport: SIZE, recordVideo: { dir: OUT, size: SIZE } });
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
