// Records the operator simulation to a video, frame-exact, with the system Chrome and ffmpeg:
//   node scripts/record.mjs --out sim.mp4 [--from 0] [--to 40] [--view drone] [--profile clean] [--fps 30]
//        [--width 1920 --height 1080] [--sbs clip.mp4 --sbs-out sbs.mp4]
//   more: --scene vd076  --run-dir runs/recon3d/vd076  --pos-res 1  --seed 1  --warm 2  --rings 0  --keep-frames  --url http://localhost:PORT
//
// It spawns the driver itself with CLOCK=manual on a free port (npx tsx src/driver.ts; RUN_DIR / SCENE / PROFILE /
// POS_RES / SEED pass through the environment or the flags), so the replay is stepped by the recorder, not by a timer, and
// the result is a function of SEED, scene and profile. With --url it drives a driver you started yourself
// (`CLOCK=manual PORT=8197 npm start`), and --profile then sends a link command first.
//
// Time. The driver steps 100 ms; the page draws between the previous and the current state (alpha 0..1). For each
// output frame i (display time from + i/fps) the recorder steps the driver until the current state is the one after that
// time, then renders at alpha = the fraction of the step, so output frame i shows exactly the state of footage time
// from + i/fps (30 fps: three frames per step) with no lag added by the interpolation. The receiver's own latency stays:
// the pose that crossed the link is one step old, as in the live page. Before `from` the replay runs --warm seconds
// (default 2) without recording, so the receiver already knows the scene when the video starts.
//
// --sbs clip.mp4: also a side-by-side, left = the real footage over the same time range (clip frame = t * 30), right =
// the recording, both 960x540. The clip must be the scene's footage from t = 0 (runs/recon3d/<run>/clip-720p.mp4).
// Fails on page errors. CHROME=/path/to/chrome overrides the browser; SOFTGL=1 renders in SwiftShader without a GPU.
import { chromium } from 'playwright-core';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(`--${k}`); return i >= 0 && i + 1 < args.length && !args[i + 1].startsWith('--') ? args[i + 1] : d; };
const flag = (k) => args.includes(`--${k}`);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STEP_S = 0.1;                                   // the driver's step
const out = opt('out', 'recon3d-sim.mp4');
const from = Number(opt('from', 0)), to = Number(opt('to', 40)), fps = Number(opt('fps', 30)), warm = Number(opt('warm', 2));
const width = Number(opt('width', 1920)), height = Number(opt('height', 1080));
const view = opt('view', 'drone');
const sbs = opt('sbs'), sbsOut = opt('sbs-out', sbs ? out.replace(/\.mp4$/, '') + '-sbs.mp4' : null);
if (!(to > from) || !(fps > 0)) { console.error('need --to > --from and --fps > 0'); process.exit(2); }
if (sbs && !fs.existsSync(sbs)) { console.error(`--sbs: no such clip ${sbs}`); process.exit(2); }

const freePort = () => new Promise((ok) => { const s = net.createServer(); s.listen(0, () => { const p = s.address().port; s.close(() => ok(p)); }); });

// ---- the driver ---------------------------------------------------------------------------------------
let driver = null, base = opt('url');
if (!base) {
  const port = await freePort();
  const env = { ...process.env, PORT: String(port), CLOCK: 'manual' };
  if (opt('profile')) env.PROFILE = opt('profile');
  if (opt('scene')) env.SCENE = opt('scene');
  if (opt('run-dir')) env.RUN_DIR = opt('run-dir');
  if (opt('pos-res')) env.POS_RES = opt('pos-res');
  if (opt('seed')) env.SEED = opt('seed');
  driver = spawn(process.execPath, [path.join(root, 'node_modules/tsx/dist/cli.mjs'), 'src/driver.ts'], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
  await new Promise((ok, fail) => {
    let log = '';
    const on = (d) => { log += d; process.stderr.write(`[driver] ${d}`); if (log.includes('recon3d driver:')) ok(); };
    driver.stdout.on('data', on); driver.stderr.on('data', on);
    driver.on('exit', (c) => fail(new Error(`driver exited (${c}) before listening`)));
  });
  base = `http://localhost:${port}`;
}
const stopDriver = () => { if (driver && !driver.killed) driver.kill('SIGTERM'); };
process.on('exit', stopDriver);

// ---- the page ------------------------------------------------------------------------------------------
const gl = process.env.SOFTGL ? ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] : ['--use-angle=vulkan', '--enable-features=Vulkan', '--ignore-gpu-blocklist'];
const browser = await chromium.launch({ executablePath: process.env.CHROME || '/usr/bin/google-chrome', args: ['--headless=new', ...gl], ignoreDefaultArgs: ['--headless'] });
const frames = fs.mkdtempSync(path.join(os.tmpdir(), 'recon3d-rec-'));
const errors = [];
let ok = false;
try {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) errors.push(m.text()); });   // a 404 is reported with its url below (favicon excepted)
  page.on('response', (r) => { if (r.status() >= 400 && !r.url().endsWith('/favicon.ico')) errors.push(`${r.status()} ${r.url()}`); });
  await page.goto(`${base}/?rec=1&view=${encodeURIComponent(view)}${opt('rings') === '0' ? '&rings=0' : ''}`);
  await page.waitForFunction(() => window.minband);
  const m = (fn, a) => page.evaluate(fn, a);
  const fail = () => { if (errors.length) throw new Error(`page errors:\n${errors.join('\n')}`); };
  if (opt('url') && opt('profile')) await m((p) => new Promise((r) => { const ws = new WebSocket(`ws://${location.host}`); ws.onopen = () => { ws.send(JSON.stringify({ cmd: 'link', profile: p })); ws.close(); r(); }; }), opt('profile'));

  // Seek (the first state also tells the page which scene to load), wait for the scene's summary and chips, warm up.
  const t0 = Math.max(0, from - warm);
  let t = await m((t) => window.minband.seek(t).then((s) => s.t), t0);
  await m(() => window.minband.whenLoaded());
  const warmSteps = Math.round((from - t0) / STEP_S);
  if (warmSteps > 0) t = await m((n) => window.minband.step(n).then((s) => s.t), warmSteps);
  if (Math.abs(t - from) > 1e-3) throw new Error(`the replay stands at ${t} s, not at --from ${from} (the scene is shorter?)`);
  fail();
  // Headless Chrome shows a white canvas for the first second or so after the page's first frames (the GPU path
  // is still coming up): render and probe one pixel of a screenshot until it is the scene's, not white.
  const probe = { x: Math.floor(width / 2), y: Math.floor(height * 0.6), width: 1, height: 1 };
  for (let k = 0, lum = 765; lum > 600; k++) {
    if (k > 100) throw new Error('the canvas stays white (WebGL not rendering?)');
    await m(() => window.minband.renderAt(0));
    const png = await page.screenshot({ type: 'png', clip: probe });
    lum = await m(async (b64) => {
      const bm = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
      const d = new OffscreenCanvas(1, 1).getContext('2d'); d.drawImage(bm, 0, 0);
      const px = d.getImageData(0, 0, 1, 1).data; return px[0] + px[1] + px[2];
    }, png.toString('base64'));
    if (lum > 600) await page.waitForTimeout(100);
  }
  console.error(`recording ${from}..${to} s at ${fps} fps, ${width}x${height}, view ${view}: ${Math.round((to - from) * fps)} frames`);

  const total = Math.round((to - from) * fps);
  let done = 0;                                       // steps taken since `from`: the current state is at from + done * STEP_S
  for (let i = 0; i < total; i++) {
    const x = i / (fps * STEP_S);                     // display time after `from`, in steps
    const j = Math.floor(x + 1e-6);                   // the state after it is the (j+1)-th step; alpha is the fraction
    while (done <= j) {
      t = await m(() => window.minband.step(1).then((s) => s.t));
      done++;
      if (Math.abs(t - (from + done * STEP_S)) > 1e-3) throw new Error(`state time ${t}, expected ${from + done * STEP_S}`);
      fail();
    }
    await m((a) => window.minband.renderAt(a), Math.min(1, Math.max(0, x - j)));
    await page.screenshot({ path: path.join(frames, `frame_${String(i).padStart(5, '0')}.jpg`), type: 'jpeg', quality: 92 });
    if ((i + 1) % 50 === 0 || i + 1 === total) console.error(`frame ${i + 1}/${total}  t ${(from + i / fps).toFixed(2)} s`);
  }
  fail();
  ok = true;
} catch (e) {
  console.error(String(e?.message ?? e));
} finally {
  await browser.close();
  stopDriver();
}
if (!ok) { fs.rmSync(frames, { recursive: true, force: true }); process.exit(1); }

// ---- ffmpeg --------------------------------------------------------------------------------------------
const ff = (a) => { const r = spawnSync('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error', ...a], { stdio: ['ignore', 'inherit', 'inherit'] }); if (r.status !== 0) { console.error(`ffmpeg failed: ${a.join(' ')}`); process.exit(1); } };
const x264 = ['-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart'];
ff(['-framerate', String(fps), '-i', path.join(frames, 'frame_%05d.jpg'), ...x264, out]);
console.log(`wrote ${out}`);
if (sbs) {
  const font = ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf', '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf'].find((f) => fs.existsSync(f));
  const label = (s) => (font ? `,drawtext=fontfile=${font}:text='${s}':x=14:y=h-th-12:fontsize=20:fontcolor=white@0.9:box=1:boxcolor=black@0.5:boxborderw=6` : '');
  if (!font) console.error('no font found: the side-by-side has no labels');
  // The clip is 30 fps and starts at footage t = 0; -ss from the input seeks to footage time `from`.
  ff(['-ss', String(from), '-t', String(to - from), '-i', sbs, '-i', out, '-filter_complex',
    `[0:v]fps=${fps},scale=960:540,setsar=1${label('drone camera (not sent in thin regimes)')}[l];`
    + `[1:v]scale=960:540,setsar=1${label('operator simulation (contact reports only)')}[r];[l][r]hstack=inputs=2:shortest=1`, ...x264, sbsOut]);
  console.log(`wrote ${sbsOut}`);
}
if (flag('keep-frames')) console.log(`frames in ${frames}`); else fs.rmSync(frames, { recursive: true, force: true });
