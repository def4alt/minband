// Headless screenshot of the page (needs playwright: uses ../../viewer/node_modules if present).
// usage: node scripts/screenshot.mjs [url] [out.png] [waitMs]
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');
let chromium;
try { ({ chromium } = createRequire(path.join(repo, 'viewer/package.json'))('playwright')); }
catch { ({ chromium } = await import('playwright')); }
const url = process.argv[2] || 'http://localhost:8090/';
const out = process.argv[3] || path.join(repo, 'runs/sidebyside/page.png');
const wait = Number(process.argv[4] || 6000);
const browser = await chromium.launch(process.env.CHANNEL ? { channel: process.env.CHANNEL } : {}); // CHANNEL=chrome for H.264 playback
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
await page.goto(url);
await page.waitForTimeout(wait);
const info = await page.evaluate(() => ({
  t: window.sbs?.msg?.t, videoT: document.getElementById('video').currentTime, paused: document.getElementById('video').paused,
  cam: !!window.sbs?.cam, events: window.sbs?.events.length, frames: window.sbs?.frames.length, rx: window.sbs?.msg?.rx?.contacts?.length,
  readyState: document.getElementById('video').readyState,
}));
console.log(JSON.stringify(info));
if (errors.length) console.log('page errors:', errors);
await page.screenshot({ path: out });
console.log('wrote', out);
await browser.close();
