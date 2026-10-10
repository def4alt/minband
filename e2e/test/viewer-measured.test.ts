// With a measured H.264 table (runs/baseline_a.json, e.g. from tools/footage/h264.sh or the phone),
// the viewer's video-on-this-link panel shows the measured bitrate and drops "configured".
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Browser } from 'playwright';
import { startStack, startViewer, type Stack } from '../lib/stack.ts';
import { launch, openPage } from '../lib/browser.ts';

const file = join(mkdtempSync(join(tmpdir(), 'minband-e2e-')), 'baseline_a.json');
writeFileSync(file, JSON.stringify({ entries: [{ id: 'h264_720p', bps: 2_943_559, source: 'measured: e2e fixture' }] }));
let s: Stack, viewer: Awaited<ReturnType<typeof startViewer>>, browser: Browser;
before(async () => { s = await startStack({ server: { MINBAND_BASELINE_A: file } }); viewer = await startViewer(s.server); browser = await launch(); });
after(async () => { await browser?.close(); await viewer?.stop(); await s?.stop(); });

test('measured Baseline A reaches the video panel without the "configured" label', async () => {
  const { page, errors } = await openPage(browser, viewer.url + '?stage=1');
  await page.waitForFunction(() => /2\.94 Mbps/.test(document.getElementById('rawRate')?.textContent ?? ''), null, { timeout: 15_000 });
  assert.doesNotMatch(await page.textContent('#rawRate') ?? '', /configured/);
  await page.close();
  assert.deepEqual(errors, []);
});
