// Headless screenshots of the running page (npm start first), with the system Chrome:
//   node scripts/screenshot.mjs [out.png] [--scene vd182] [--at 6] [--profile lora] [--view chase] [--wait 4] [--eval 'js']
// Fails on page errors. CHROME=/path/to/chrome overrides the browser.
import { chromium } from 'playwright-core';

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(`--${k}`); return i >= 0 ? args[i + 1] : d; };
const out = args[0] && !args[0].startsWith('--') ? args[0] : 'recon3d.png';
const url = process.env.URL || `http://localhost:${process.env.PORT || 8092}/`;
// WebGL in headless Chrome: the GPU through Vulkan; SOFTGL=1 for SwiftShader on a machine without one.
const gl = process.env.SOFTGL ? ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] : ['--use-angle=vulkan', '--enable-features=Vulkan', '--ignore-gpu-blocklist'];
const browser = await chromium.launch({ executablePath: process.env.CHROME || '/usr/bin/google-chrome', args: ['--headless=new', ...gl], ignoreDefaultArgs: ['--headless'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error' && !/404/.test(m.text())) errors.push(m.text()); });
page.on('response', (r) => { if (r.status() >= 400 && !r.url().endsWith('/favicon.ico')) errors.push(`${r.status()} ${r.url()}`); });
await page.goto(url);
await page.waitForTimeout(1500);
const send = (m) => page.evaluate((m) => new Promise((ok) => { const ws = new WebSocket(`ws://${location.host}`); ws.onopen = () => { ws.send(JSON.stringify(m)); ws.close(); ok(); }; }), m);
if (opt('scene')) { await send({ cmd: 'scene', name: opt('scene') }); await page.waitForTimeout(2500); }
if (opt('profile')) await send({ cmd: 'link', profile: opt('profile') });
if (opt('at')) await send({ cmd: 'seek', t: Number(opt('at')) });
if (opt('view')) await page.click(`#views button[data-v="${opt('view')}"]`);
await page.waitForTimeout(Number(opt('wait', 4)) * 1000);
// --eval 'js': run in the page before the shot (window.minband has camera, controls, objects, state).
if (opt('eval')) { await page.evaluate(opt('eval')); await page.waitForTimeout(600); }
await page.screenshot({ path: out });
await browser.close();
if (errors.length) { console.error(errors.join('\n')); process.exit(1); }
console.log(`wrote ${out}`);
