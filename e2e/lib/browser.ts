// Headless Chromium for the viewer tests. The pre-installed browser matches playwright 1.56.1;
// SwiftShader gives the Three.js scene a WebGL context without a GPU.
import { chromium, type Browser, type Page } from 'playwright';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { ROOT } from './stack.ts';

export const SHOTS = join(ROOT, 'runs', 'e2e');

export async function launch(): Promise<Browser> {
  return chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
}

/** Page with console errors and uncaught exceptions collected (external font fetches ignored). */
export async function openPage(browser: Browser, url: string, size = { width: 1600, height: 900 }) {
  const page = await browser.newPage({ viewport: size, deviceScaleFactor: 1 });
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error' && !/fonts\.(googleapis|gstatic)\.com|ERR_(TUNNEL|PROXY|NAME|CONNECTION)/.test(m.text() + (m.location().url ?? ''))) errors.push(`console: ${m.text()}`); });
  page.on('requestfailed', r => { if (!/fonts\.(googleapis|gstatic)\.com/.test(r.url())) errors.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`); });
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  return { page, errors };
}

export async function shot(page: Page, name: string) {
  mkdirSync(SHOTS, { recursive: true });
  const path = join(SHOTS, `${name}.png`);
  await page.screenshot({ path });
  return path;
}
