// The Pi link box path, on this machine: tools/pi-link.sh installs its classifier on `lo` for the
// server's UDP port, so sim datagrams are rate-limited by the kernel before the server's socket
// (OS-level shaping, outside our own server). Containers without sch_netem/sch_prio use the
// script's test hooks: htb root and a tbf leaf as the radio's rate limit. Needs root and tc.
import { test, after, before } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { ROOT, startStack, getJson, sleep, type Stack } from '../lib/stack.ts';

const SCRIPT = join(ROOT, 'tools', 'pi-link.sh');
const tcOk = spawnSync('tc', ['-V']).status === 0 && process.getuid?.() === 0;
const loFree = tcOk && /qdisc noqueue 0: root/.test(spawnSync('tc', ['qdisc', 'show', 'dev', 'lo']).stdout.toString());
const skip = !tcOk ? 'needs root and tc' : !loFree ? 'lo already has a root qdisc' : false;

let s: Stack, env: Record<string, string>;
const link = (profile: string) => {
  const r = spawnSync(SCRIPT, [profile], { env: { ...process.env, ...env }, encoding: 'utf8' });
  assert.equal(r.status, 0, `${profile}: ${r.stderr}${r.stdout}`);
  return r.stdout;
};

before(async () => {
  if (skip) return;
  s = await startStack({ historyMs: 20_000 });
  const prio = spawnSync('tc', ['qdisc', 'add', 'dev', 'lo', 'root', 'handle', '1:', 'prio', 'bands', '4']).status === 0;
  if (prio) spawnSync('tc', ['qdisc', 'del', 'dev', 'lo', 'root']);
  env = {
    UP_DEV: 'lo', DOWN_DEV: 'lo', PORT: String(s.server.udpPort), STATE_DIR: join(ROOT, 'runs', 'e2e', 'pi-link-state'),
    ...(prio ? {} : { ROOT_QDISC_OVERRIDE: 'htb' }),
    // 2 kbit/s with a 4-packet-ish buffer, standing in for netem `rate 2kbit limit 4`.
    LEAF_QDISC_OVERRIDE: 'tbf rate 2kbit burst 400 latency 300ms',
  };
});
after(async () => {
  if (skip) return;
  spawnSync(SCRIPT, ['clear'], { env: { ...process.env, ...env } });
  spawnSync('tc', ['qdisc', 'del', 'dev', 'lo', 'root']);
  await s?.stop();
});

test('kernel-shaped 2 kbit/s link: the server receives at most the link rate and the twin holds', { skip }, async () => {
  await s.feed.until('device up', x => x.devices.find((d: any) => d.deviceId === 100)?.entities.length >= 3, 10_000);
  const out = link('lora');
  assert.match(out, /api\/budget\?bps=1500/, 'script prints the edge budget for the profile');
  await getJson(`${s.server.api}/api/budget?bps=1500`);
  await sleep(15_000);
  const m = await getJson(`${s.server.api}/api/metrics`);
  const d = m.devices.find((x: any) => x.deviceId === 100);
  const tcStats = spawnSync('tc', ['-s', 'qdisc', 'show', 'dev', 'lo']).stdout.toString();
  console.log(`kernel lora link: delivered ${Math.round(d.bps)} bit/s, offered by edge ${Math.round(d.offeredBps)} bit/s`);
  assert.ok(d.bps <= 2300, `delivered ${d.bps} bit/s through a 2 kbit/s kernel link\n${tcStats}`);
  assert.ok(d.bps > 200, `still receiving: ${d.bps} bit/s`);
  assert.equal(d.staleEntities, 0, 'no stale entities');
  assert.ok(d.entities >= 3);
  assert.match(tcStats, /tbf/, 'tbf leaf installed');
});

test('clear restores the unshaped link', { skip }, async () => {
  link('clear');
  assert.match(spawnSync('tc', ['qdisc', 'show', 'dev', 'lo']).stdout.toString(), /noqueue/);
  await getJson(`${s.server.api}/api/budget?bps=0`);
  await sleep(4000);
  const m = await getJson(`${s.server.api}/api/metrics`);
  assert.equal(m.devices.find((x: any) => x.deviceId === 100).staleEntities, 0);
});
