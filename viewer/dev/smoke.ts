// Viewer smoke test: boots the mock snapshot server and the Vite dev server on private ports, drives
// the mock through the phases that exercise every visual, screenshots each, and fails on any
// console error or page error.
//
//   npm run smoke                                   # shots in /tmp/minband-viewer-shots
//   SMOKE_OUT=/some/dir MOCK_PORT=18080 VITE_PORT=15173 npm run smoke
//   SMOKE_ONLY=stale,recovery npm run smoke         # a subset
//
// Chromium comes from PLAYWRIGHT_BROWSERS_PATH (the playwright version is pinned to its build). It
// renders WebGL with SwiftShader. Google Fonts are stubbed so the run needs no network (set
// SMOKE_FONTS=net to load them); the shots then use the fallback fonts.
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { chromium, type Browser, type Page } from 'playwright';
import { createServer } from 'vite';

const OUT = process.env.SMOKE_OUT ?? '/tmp/minband-viewer-shots';
const MOCK_PORT = Number(process.env.MOCK_PORT ?? 18080), VITE_PORT = Number(process.env.VITE_PORT ?? 15173);
const MOCK = `http://localhost:${MOCK_PORT}`, APP = `http://localhost:${VITE_PORT}/`;
const ONLY = process.env.SMOKE_ONLY?.split(',');
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
mkdirSync(OUT, { recursive: true });

async function waitFor(url: string, ms = 15_000) {
  const end = Date.now() + ms;
  for (;;) {
    try { if ((await fetch(url)).ok) return; } catch { /* not up yet */ }
    if (Date.now() > end) throw new Error(`timed out waiting for ${url}`);
    await sleep(200);
  }
}
const control = async (q: string) => (await fetch(`${MOCK}/mock?${q}`)).json() as Promise<{ phase: string; at: number }>;

/** Chromium from PLAYWRIGHT_BROWSERS_PATH, else the binary under /opt/pw-browsers. */
async function launch(): Promise<Browser> {
  const args = ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'];
  try { return await chromium.launch({ args }); } catch (e) {
    const root = '/opt/pw-browsers', dir = existsSync(root) ? readdirSync(root).find(d => /^chromium-\d+$/.test(d)) : undefined;
    if (!dir) throw e;
    return chromium.launch({ args, executablePath: join(root, dir, 'chrome-linux', 'chrome') });
  }
}

const mock = spawn(process.execPath, ['--import', 'tsx', 'dev/mock-server.ts'], { env: { ...process.env, MOCK_PORT: String(MOCK_PORT) }, stdio: ['ignore', 'ignore', 'inherit'] });
let failed = false;
const errors: string[] = [];
let browser: Browser | null = null;
process.env.VITE_WS_URL = `ws://localhost:${MOCK_PORT}`;
const vite = await createServer({ server: { port: VITE_PORT, strictPort: true }, logLevel: 'warn' });
try {
  await waitFor(`${MOCK}/mock`);
  await vite.listen();
  browser = await launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
  page.on('console', m => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
  if (process.env.SMOKE_FONTS !== 'net') await page.route(/fonts\.(googleapis|gstatic)\.com/, r => r.fulfill({ status: 200, contentType: 'text/css', body: '' }));

  /**
   * Close the old page's socket, jump the mock (its backlog then reaches the new page), load, wait.
   * The mock is frozen until the page has drawn its first snapshot, so a slow load (software GL)
   * does not eat into a 10 s blackout; `waitMs` is simulated time the page then watches.
   */
  const open = async (p: Page, mockQuery: string, query: string, waitMs: number) => {
    await p.goto('about:blank');
    await control(`geo=1&measured=0&legacy=0&${mockQuery}&freeze=1`); // later keys win: shots override the defaults
    await p.goto(`${APP}?${query}`);
    await p.waitForFunction(() => document.getElementById('status')?.hidden === true, null, { timeout: 20_000 });
    await sleep(300);
    await control('freeze=0');
    await sleep(waitMs);
    await control('freeze=1'); // screenshots are slow under software GL: hold the state still for them
  };
  /** The scene object main.ts exposes on the dev server. */
  const SCENE = 'window.__minband.scene';
  /** Click an entity (a click without drag pins its tag) and return the visible tag text. */
  const pinTag = async (p: Page, gid: string) => {
    const pos = await p.evaluate(`${SCENE}.screenOf(${JSON.stringify(gid)})`) as { x: number; y: number } | null;
    const box = await p.locator('#scene').boundingBox();
    if (!pos || !box) throw new Error(`entity ${gid} not on screen`);
    await p.mouse.click(box.x + pos.x, box.y + pos.y);
    await sleep(400);
    const tag = await p.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>('.tag3d')).filter(e => e.offsetParent && getComputedStyle(e).display !== 'none' && e.textContent).map(e => e.textContent).join(' | '));
    console.log(`  tag: ${tag}`);
    return tag;
  };
  const expectWide = async (p: Page, want: boolean) => {
    const wide = await p.evaluate(`${SCENE}.wide`);
    if (wide !== want) throw new Error(`scene ${wide ? 'wide' : 'room'}, expected ${want ? 'wide' : 'room'}`);
  };
  const report = async (p: Page, name: string) => {
    const text = (id: string) => p.evaluate(i => document.getElementById(i)?.textContent?.replace(/\s+/g, ' ').trim() ?? '', id);
    console.log(`${name.padEnd(22)} edge: ${await text('cEdge')} | link: ${await text('cLink')} | twin: ${await text('cTwin')}`);
  };
  const SHOTS: { name: string; size?: { width: number; height: number }; run: (p: Page) => Promise<void> }[] = [
    { name: 'normal', run: async p => { await open(p, 'phase=clean&at=6&hold=1', 'stage=0&details=0', 3000); } },
    { name: 'lora-waterfall', run: async p => { await open(p, 'phase=lora&at=4&hold=1', 'stage=1&details=0', 8000); } },
    // Blackout on the clean profile: coasting from 2.5 s, stale from 6 s, link back at 10 s.
    { name: 'blackout-coasting', run: async p => { await open(p, 'phase=blackout&at=2.4&hold=1', 'stage=0&details=0', 2600); } },
    { name: 'stale', run: async p => { await open(p, 'phase=blackout&at=6&hold=1', 'stage=1&details=0', 2600); } },
    { name: 'recovery', run: async p => { await open(p, 'phase=blackout&at=7.5&hold=1', 'stage=1&details=0', 4000); } },
    {
      // lora: a 6.3 KB frame takes 25 s at 2 kbit/s; the page starts a frame when it opens.
      name: 'v1-panel', run: async p => {
        await open(p, 'phase=lora&at=1&hold=1', 'stage=1&details=0', 12_000);
        await p.locator('#camera').screenshot({ path: join(OUT, 'v1-panel-crop.png') });
      },
    },
    {
      name: 'geo-tag', run: async p => {
        await open(p, 'phase=lora&at=6&hold=1', 'stage=0&details=0', 2500);
        const tag = await pinTag(p, 'g1');
        if (!/\d{2}[A-Z] [A-Z]{2} \d{5} \d{5}/.test(tag)) throw new Error(`no MGRS in the tag: ${tag}`);
      },
    },
    {
      // DETAILS scrolled down: link activity strip, device panel (airtime share, heartbeat), log.
      name: 'details', size: { width: 1600, height: 1200 }, run: async p => {
        await open(p, 'phase=lora&at=8&hold=1', 'stage=0&details=1', 6000);
        await p.evaluate(() => { const s = document.getElementById('side')!; s.scrollTop = s.scrollHeight; });
        await sleep(300);
      },
    },
    // An older server (no hackathon fields): the viewer falls back to scenarios and draws no rings.
    { name: 'legacy', run: async p => { await open(p, 'phase=lora&at=8&hold=1&legacy=1', 'stage=1&details=1', 3000); } },
    { name: 'measured', run: async p => { await open(p, 'phase=hf&at=4&hold=1&measured=1', 'stage=0&details=1', 3000); } },
    { name: 'no-geo', run: async p => { await open(p, 'phase=clean&at=6&hold=1&geo=0', 'stage=0&details=0', 2000); } },
    { name: 'narrow', size: { width: 390, height: 844 }, run: async p => { await open(p, 'phase=blackout&at=3&hold=1', 'stage=0&details=0', 2500); } },
    // Wide area (real-drone scale): grid, magnified glyphs, the drone's frustum and nadir, FRAME.
    { name: 'wide', run: async p => { await open(p, 'phase=wide&at=6&hold=1', 'stage=0&details=0', 3000); await expectWide(p, true); } },
    { name: 'wide-stage', run: async p => { await open(p, 'phase=wide&at=12&hold=1', 'stage=1&details=0', 3000); await expectWide(p, true); } },
    {
      // A parked car's tag names the class; F re-frames after an orbit.
      name: 'wide-tag', run: async p => {
        await open(p, 'phase=wide&at=6&hold=1', 'stage=0&details=0', 2000);
        const box = await p.locator('#scene').boundingBox();
        if (!box) throw new Error('no scene');
        await p.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        await p.mouse.down(); await p.mouse.move(box.x + box.width / 2 + 160, box.y + box.height / 2, { steps: 8 }); await p.mouse.up();
        await sleep(300);
        await p.keyboard.press('f');
        await sleep(900);
        const tag = await pinTag(p, 'g10');
        if (!/^car · g10 · /.test(tag)) throw new Error(`the car's tag does not name it: ${tag}`);
        if (!(await p.isVisible('#frame'))) throw new Error('FRAME control hidden in wide mode');
      },
    },
    {
      // Back to room scale when the entities gather again (hysteresis 2.5 s, then the glide).
      name: 'wide-to-room', run: async p => {
        await open(p, 'phase=wide&at=6&hold=1', 'stage=0&details=0', 1500);
        await expectWide(p, true);
        await control('phase=clean&at=6&hold=1&freeze=0');
        await sleep(4500);
        await control('freeze=1');
        await expectWide(p, false);
        if (await p.isVisible('#frame')) throw new Error('FRAME control still shown in room mode');
      },
    },
  ];
  for (const s of SHOTS) {
    if (ONLY && !ONLY.includes(s.name)) continue;
    await page.setViewportSize(s.size ?? { width: 1600, height: 900 });
    await s.run(page);
    await page.screenshot({ path: join(OUT, `${s.name}.png`) });
    await report(page, s.name);
  }
  // Toggles that need a user gesture: the click must start without errors.
  await page.click('#detailsToggle').catch(() => {});
  if (await page.isVisible('#click')) { await page.click('#click'); await sleep(1000); await page.click('#click'); }
} catch (e) {
  failed = true;
  console.error(e);
} finally {
  await browser?.close();
  await vite.close();
  mock.kill('SIGTERM');
}
if (errors.length) { failed = true; console.error(`\n${errors.length} console/page error(s):\n${errors.join('\n')}`); }
console.log(`\n${failed ? 'FAIL' : 'ok'}: screenshots in ${OUT}`);
process.exit(failed ? 1 : 0);
