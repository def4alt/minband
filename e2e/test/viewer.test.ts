// The viewer in headless Chromium against the real server and a sim edge: it connects, renders the
// twin (WebGL), and its readouts follow the live snapshot.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import type { Browser } from 'playwright';
import { startStack, startViewer, getJson, sleep, type Stack } from '../lib/stack.ts';
import { launch, openPage, shot } from '../lib/browser.ts';

let s: Stack, viewer: Awaited<ReturnType<typeof startViewer>>, browser: Browser;
before(async () => {
  s = await startStack();
  viewer = await startViewer(s.server);
  browser = await launch();
});
after(async () => { await browser?.close(); await viewer?.stop(); await s?.stop(); });

test('viewer connects, renders the twin and shows the live rate without errors', async () => {
  const { page, errors } = await openPage(browser, viewer.url);
  await page.waitForFunction(() => document.querySelector('#scene canvas') !== null, null, { timeout: 15_000 });
  await page.waitForFunction(() => /1 device/.test(document.getElementById('cEdge')?.textContent ?? '') && document.getElementById('status')?.hidden, null, { timeout: 15_000 });
  await sleep(2500);
  const bps = await page.textContent('#bps');
  assert.match(bps ?? '', /\d+\.\d/);
  assert.ok(Number.parseFloat(bps!) > 0, `bps readout ${bps}`);
  const gl = await page.evaluate(() => { const c = document.querySelector('#scene canvas') as HTMLCanvasElement; return !!(c.getContext('webgl2') || c.getContext('webgl')); });
  assert.ok(gl, 'WebGL context');
  console.log(await shot(page, 'viewer-live'));
  await page.close();
  assert.deepEqual(errors, []);
});

test('blackout from the API shows up in the viewer as stale entities, then recovers', async () => {
  const { page, errors } = await openPage(browser, viewer.url);
  await page.waitForFunction(() => document.getElementById('status')?.hidden, null, { timeout: 15_000 });
  await getJson(`${s.server.api}/api/shaper?enabled=1&loss=1&revertAfterMs=9000`);
  await page.waitForFunction(() => /blackout/i.test(document.getElementById('scenarioName')?.textContent ?? document.body.textContent ?? ''), null, { timeout: 5000 });
  await sleep(7000);
  console.log(await shot(page, 'viewer-blackout'));
  await sleep(5000);
  console.log(await shot(page, 'viewer-recovered'));
  await page.close();
  assert.deepEqual(errors, []);
});

test('profile buttons drive the server link; STAGE shows the link activity strip and video-on-this-link', async () => {
  const { page, errors } = await openPage(browser, viewer.url + '?stage=1');
  await page.waitForFunction(() => document.getElementById('status')?.hidden, null, { timeout: 15_000 });
  await page.waitForSelector('#scenario button[data-profile="lora"]', { timeout: 10_000 });
  await page.click('#scenario button[data-profile="lora"]');
  await page.waitForFunction(() => /lora/.test(document.getElementById('cLink')?.textContent ?? '') && /airtime/.test(document.getElementById('cLink')?.textContent ?? ''), null, { timeout: 8000 });
  const v = await getJson(`${s.server.api}/api/link`);
  assert.equal(v.profile, 'lora', 'the click reached the server');
  assert.ok(await page.isVisible('#waterfall'), 'link activity strip visible in STAGE');
  assert.ok(await page.isVisible('#frameClock'), 'video-on-this-link countdown visible');
  await sleep(6000);
  assert.match(await page.textContent('#vlThumb') ?? '', /\d/, 'thumbnail competitor computed');
  console.log(await shot(page, 'viewer-stage-lora'));
  await getJson(`${s.server.api}/api/link?profile=clean`);
  await page.close();
  assert.deepEqual(errors, []);
});

test('blackout: the credits say coasting before stale, the key says dismount', async () => {
  const { page, errors } = await openPage(browser, viewer.url);
  await page.waitForFunction(() => document.getElementById('status')?.hidden, null, { timeout: 15_000 });
  assert.match(await page.textContent('#key') ?? '', /dismount/i);
  await sleep(3000);
  await getJson(`${s.server.api}/api/shaper?enabled=1&loss=1&revertAfterMs=9000`);
  await page.waitForFunction(() => /coasting/.test(document.getElementById('cEdge')?.textContent ?? ''), null, { timeout: 6000 });
  await sleep(1500);
  console.log(await shot(page, 'viewer-coasting-rings'));
  await page.waitForFunction(() => !/coasting/.test(document.getElementById('cEdge')?.textContent ?? ''), null, { timeout: 15_000 });
  await page.close();
  assert.deepEqual(errors, []);
});
