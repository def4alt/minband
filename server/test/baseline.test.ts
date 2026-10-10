import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { BASELINE_A_CONFIGURED, CONFIGURED_SOURCE, defaultBaselineAFile, mergeBaselineA } from '../src/baseline.js';
import { World } from '../src/world.js';
import { FakeClock } from './fake.js';

test('Baseline A: configured until runs/baseline_a.json exists, then merged by id and reloaded on change', () => {
  const dir = mkdtempSync(join(tmpdir(), 'minband-baseline-'));
  try {
    const file = join(dir, 'baseline_a.json');
    const clock = new FakeClock();
    const world = new World({ now: clock.now, baselineAFile: file });
    let s = world.snapshot();
    assert.deepEqual(s.baselineA.map(b => [b.id, b.bps, b.measured]), [['h264_720p', 1_500_000, false], ['h264_480p', 500_000, false], ['h264_360p', 250_000, false]]);
    assert.ok(s.baselineA.every(b => b.source === CONFIGURED_SOURCE));
    assert.deepEqual([s.baselines.h264_720p_bps, s.baselines.h264_480p_bps], [1_500_000, 500_000], 'legacy field kept');

    writeFileSync(file, JSON.stringify({ entries: [{ id: 'h264_720p', bps: 1_234_567 }, { id: 'h264_1080p', bps: 4_000_000, label: 'H.264 1080p', resolution: '1920x1080' }] }));
    clock.advance(1_000);
    assert.equal(world.snapshot().baselineA[0].bps, 1_500_000, 'checked at most every 2 s');
    clock.advance(1_000);
    s = world.snapshot();
    assert.deepEqual(s.baselineA[0], { id: 'h264_720p', label: 'H.264 720p', bps: 1_234_567, measured: true, source: `measured (${file})` });
    assert.deepEqual(s.baselineA.map(b => [b.id, b.measured]), [['h264_720p', true], ['h264_480p', false], ['h264_360p', false], ['h264_1080p', true]]);
    assert.equal(s.baselineA[3].label, 'H.264 1080p');
    assert.deepEqual([s.baselines.h264_720p_bps, s.baselines.h264_480p_bps], [1_234_567, 500_000]);

    writeFileSync(file, '{"entries":[{"id":"h264_480p"}]}'); // no bps: rejected like tools/eval
    assert.ok(world.baselineA.get(true).every(b => !b.measured));
    assert.match(world.baselineA.error!, /positive bps/);
    writeFileSync(file, '{"entries":[{"id":"h264_480p","bps":480000,"source":"VideoToolbox, 60 s","fps":24}]}');
    const t = world.baselineA.get(true);
    assert.deepEqual([t[1].bps, t[1].measured, t[1].source, t[1].fps, world.baselineA.error], [480_000, true, 'VideoToolbox, 60 s', 24, null]);
    assert.equal(t[0].fps, undefined, 'no fps given: absent (30)');
    writeFileSync(file, '{"entries":[{"id":"h264_480p","bps":480000,"fps":0}]}');
    assert.match((world.baselineA.get(true), world.baselineA.error!), /fps must be a positive number/);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('Baseline A: merge rule and file location', () => {
  assert.throws(() => mergeBaselineA('{"entries":[{"bps":5}]}', 'f'), /id and a positive bps/);
  assert.throws(() => mergeBaselineA('not json', 'f'));
  assert.deepEqual(mergeBaselineA('{}', 'f'), BASELINE_A_CONFIGURED);
  const repo = fileURLToPath(new URL('../../', import.meta.url));
  assert.equal(defaultBaselineAFile({}), join(repo, 'runs', 'baseline_a.json'));
  assert.equal(defaultBaselineAFile({ MINBAND_RUNS: '/data/runs' }), '/data/runs/baseline_a.json');
  assert.equal(defaultBaselineAFile({ MINBAND_RUNS: '/data/runs', MINBAND_BASELINE_A: 'x/a.json' }), resolve('x/a.json'));
});
