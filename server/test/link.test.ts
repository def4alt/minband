import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CONTESTED, LORA_LONGFAST, Link, PROFILES, airtimeMs, findProfile, linkError, loraAirtimeMs, serialModel, type LoraModel } from '../src/link.js';
import { DEFAULT_SHAPER, Shaper, seededRng } from '../src/shaper.js';
import { FakeClock } from './fake.js';

const close = (a: number, b: number, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const lora = (o: Partial<LoraModel>): LoraModel => ({ ...LORA_LONGFAST, ...o });
const SF7 = lora({ sf: 7, bwHz: 125_000, preamble: 8 });

test('LoRa time on air: Semtech formula vectors', () => {
  // Meshtastic LongFast, 16 B: 20.25 preamble + 23 payload symbols of 8.192 ms.
  close(loraAirtimeMs(LORA_LONGFAST, 16), 354.304);
  // SF7 / 125 kHz / CR 4/5 / 8-symbol preamble / explicit header / CRC: 20 B = 56.576 ms (Semtech calculator 56.58 ms).
  close(loraAirtimeMs(SF7, 20), 56.576);
  // Implicit header, no CRC: 10 B -> ceil(60/28) = 3 blocks -> 23 payload symbols.
  close(loraAirtimeMs({ ...SF7, explicitHeader: false, crc: false }, 10), 36.096);
  // CR 4/8: 7 blocks x 8 symbols.
  close(loraAirtimeMs({ ...SF7, cr: 8 }, 20), 78.08);
  // SF12 / 125 kHz with low data rate optimisation (32.768 ms symbols): 10 B -> ceil(76/40) = 2 blocks.
  close(loraAirtimeMs(lora({ sf: 12, bwHz: 125_000, preamble: 8, lowDataRateOptimize: true }), 10), 991.232);
  // Payload term never goes negative: header + preamble only.
  close(loraAirtimeMs({ ...lora({ sf: 12, bwHz: 125_000, preamble: 8 }), explicitHeader: false, crc: false }, 0), (8 + 4.25 + 8) * 32.768);
  // Radio framing counts as payload.
  close(loraAirtimeMs({ ...LORA_LONGFAST, overheadBytes: 16 }, 0), 354.304);
  close(airtimeMs(LORA_LONGFAST, 16), 354.304);
  assert.ok(airtimeMs(LORA_LONGFAST, 60) > airtimeMs(LORA_LONGFAST, 16));
});

test('serial time on air: (payload + framing) x bits per byte at the rate; none is free', () => {
  close(airtimeMs(serialModel(9_600), 40), 42 * 10 / 9_600 * 1000); // 43.75 ms
  close(airtimeMs(serialModel(600), 40), 700);
  close(airtimeMs({ kind: 'serial', rateBps: 9_600, bitsPerByte: 8, overheadBytes: 0 }, 12), 10);
  assert.equal(airtimeMs({ kind: 'none' }, 1000), 0);
});

test('profile table is the Pi link box table (HACKATHON_PLAN 3.3)', () => {
  const t = Object.fromEntries(PROFILES.map(p => [p.name, [p.bps, p.delayMs, p.loss, p.queue, p.budgetBps, p.airtime.kind]]));
  assert.deepEqual(t, {
    clean: [0, 0, 0, 0, 0, 'none'],
    degraded: [64_000, 20, 0.02, 20, 0, 'none'],
    hf: [9_600, 500, 0.01, 8, 8_000, 'serial'],
    lora: [2_000, 300, 0.10, 4, 1_500, 'lora'],
    telemetry: [600, 50, 0.05, 4, 450, 'serial'],
    contested: [2_000, 300, 0.10, 4, 1_500, 'lora'],
    blackout: [0, 0, 1, 0, 0, 'none'],
  });
  assert.deepEqual(findProfile('hf')!.airtime, { kind: 'serial', rateBps: 9_600, bitsPerByte: 10, overheadBytes: 2 });
  assert.deepEqual(findProfile('lora')!.airtime, LORA_LONGFAST);
  assert.ok(PROFILES.every(p => p.label.length > 0));
});

function mk(rng: () => number = () => 0.5) {
  const clock = new FakeClock();
  const shaper = new Shaper({ now: clock.now, schedule: clock.schedule, cancel: clock.cancel, rng: () => 0.99 });
  const st = { budget: -1, notes: [] as string[] };
  const link = new Link({ shaper, setBudget: b => { st.budget = b; }, note: l => st.notes.push(l), now: clock.now, rng, schedule: clock.schedule, cancel: clock.cancel });
  return { clock, shaper, link, st };
}
/** Read through a call: assert.deepEqual on `link.model` would narrow its type for later reads. */
const kind = (l: Link) => l.model.kind;

test('applying a profile sets the shaper, the edge budget and the airtime model', () => {
  const { shaper, link, st } = mk();
  assert.equal(link.profile, 'clean');
  assert.equal(link.apply('lora'), null);
  assert.deepEqual(shaper.config, { ...DEFAULT_SHAPER, enabled: true, bps: 2_000, delayMs: 300, loss: 0.1, queue: 4 });
  assert.equal(st.budget, 1_500);
  assert.deepEqual(link.model, LORA_LONGFAST);
  assert.equal(link.apply('telemetry'), null);
  assert.deepEqual([shaper.config.bps, shaper.config.delayMs, shaper.config.loss, shaper.config.queue, st.budget], [600, 50, 0.05, 4, 450]);
  assert.equal(kind(link), 'serial');
  link.apply('blackout');
  assert.deepEqual([shaper.config.enabled, shaper.config.loss, st.budget, kind(link)], [true, 1, 0, 'none']);
  link.apply('clean');
  assert.deepEqual(shaper.config, DEFAULT_SHAPER);
  assert.deepEqual([st.budget, kind(link), link.profile], [0, 'none', 'clean']);
  const v = link.view({ airtimeShare: 0.25, msgsPerSec: 2 });
  assert.deepEqual([v.profile, v.airtimeShare, v.msgsPerSec, v.profiles.length, v.as, v.contested], ['clean', 0.25, 2, 7, undefined, undefined]);
  assert.ok(st.notes.some(n => n.includes('lora')));
});

test("'external': shaper off, budget and model of `as`; invalid names change nothing", () => {
  const { clock, shaper, link, st } = mk();
  link.apply('lora');
  assert.equal(link.apply('external', 'hf'), null);
  assert.deepEqual(shaper.config, DEFAULT_SHAPER);
  assert.deepEqual([link.profile, link.as, st.budget], ['external', 'hf', 8_000]);
  assert.deepEqual(link.model, serialModel(9_600));
  assert.equal(link.view({ airtimeShare: 0, msgsPerSec: 0 }).as, 'hf');
  link.apply('external', 'contested'); // the box runs the jammer: no loop here
  assert.deepEqual([link.model.kind, st.budget, clock.pending], ['lora', 1_500, 0]);
  assert.equal(link.view({ airtimeShare: 0, msgsPerSec: 0 }).contested, undefined);
  link.apply('external');
  assert.deepEqual([link.as, link.model.kind, st.budget], ['clean', 'none', 0]);

  link.apply('hf');
  const before = JSON.stringify([shaper.config, link.profile, link.as, link.model, st.budget]);
  for (const [p, as] of [['nope'], ['lora', 'hf'], ['external', 'external'], ['external', 'custom'], [42], ['custom']] as [unknown, unknown?][]) {
    assert.ok(link.apply(p, as), `${p} as ${as} should be rejected`);
    assert.ok(linkError(p, as));
  }
  assert.equal(JSON.stringify([shaper.config, link.profile, link.as, link.model, st.budget]), before);
});

test('contested: lora alternating with random blackouts; stops when another profile is applied', () => {
  const seq = [0, 1, 0.5, 0.25]; let i = 0;
  const { clock, shaper, link, st } = mk(() => seq[i++ % seq.length]);
  link.apply('contested');
  assert.deepEqual([shaper.config.loss, shaper.config.bps, st.budget, link.model.kind], [0.1, 2_000, 1_500, 'lora']);
  assert.deepEqual(link.view({ airtimeShare: 0, msgsPerSec: 0 }).contested, { blackout: false, switchInMs: 3_000 }); // rng 0 -> shortest on
  clock.advance(2_999); assert.equal(shaper.config.loss, 0.1);
  clock.advance(1); assert.equal(shaper.config.loss, 1);
  assert.equal(shaper.config.bps, 2_000, 'blackout is lora at 100 % loss');
  assert.deepEqual(link.view({ airtimeShare: 0, msgsPerSec: 0 }).contested, { blackout: true, switchInMs: 5_000 }); // rng 1 -> longest blackout
  assert.equal(link.profile, 'contested');
  clock.advance(5_000); assert.equal(shaper.config.loss, 0.1);
  assert.equal(link.view({ airtimeShare: 0, msgsPerSec: 0 }).contested!.switchInMs, 5_500);
  clock.advance(5_500); assert.equal(shaper.config.loss, 1);
  link.apply('hf');
  assert.equal(clock.pending, 0, 'loop timer cancelled');
  clock.advance(60_000);
  assert.deepEqual([shaper.config.loss, shaper.config.bps, link.profile], [0.01, 9_600, 'hf']);
  assert.equal(link.view({ airtimeShare: 0, msgsPerSec: 0 }).contested, undefined);

  // Over ten minutes with a seeded RNG every phase stays within its bounds.
  const r = mk(seededRng(3));
  r.link.apply('contested');
  const runs: [number, number][] = []; let cur = r.shaper.config.loss, len = 0;
  for (let t = 0; t < 600_000; t += 100) {
    r.clock.advance(100); len += 100;
    if (r.shaper.config.loss !== cur) { runs.push([cur, len]); cur = r.shaper.config.loss; len = 0; }
  }
  const off = runs.filter(x => x[0] === 1).map(x => x[1]), on = runs.slice(1).filter(x => x[0] !== 1).map(x => x[1]);
  assert.ok(off.length > 40 && on.length > 40, `${off.length} blackouts`);
  assert.ok(off.every(ms => ms >= CONTESTED.blackoutMs[0] && ms <= CONTESTED.blackoutMs[1] + 100), String(off));
  assert.ok(on.every(ms => ms >= CONTESTED.onMs[0] && ms <= CONTESTED.onMs[1] + 100), String(on));
  r.link.stop();
  assert.equal(r.clock.pending, 0);
});

test("manual shaper change: 'custom' with the model kept; timed overrides keep the profile", () => {
  const { clock, shaper, link } = mk(() => 0);
  link.apply('lora');
  link.manual({ loss: 1 }, 10_000); // the viewer's blackout preset
  assert.deepEqual([link.profile, shaper.config.loss], ['lora', 1]);
  clock.advance(10_000);
  assert.deepEqual([link.profile, shaper.config.loss], ['lora', 0.1]);
  link.manual({});
  assert.equal(link.profile, 'lora', 'no fields: no change');
  link.manual({ delayMs: 800, bogus: 1 } as never);
  assert.deepEqual([link.profile, link.model.kind, shaper.config.delayMs, shaper.config.loss], ['custom', 'lora', 800, 0.1]);

  // Leaving contested mid-blackout by hand does not keep the jammer's 100 % loss.
  link.apply('contested');
  clock.advance(3_000);
  assert.equal(shaper.config.loss, 1);
  link.manual({ delayMs: 100 });
  assert.deepEqual([link.profile, shaper.config.loss, shaper.config.delayMs, clock.pending], ['custom', 0.1, 100, 0]);
});
