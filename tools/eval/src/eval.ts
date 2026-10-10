// One entry point: synth -> sweep -> baselines -> charts.
//   npm run eval                                   # synthetic scenarios (runs/synth -> runs/eval)
//   npm run eval -- --gt runs/phone/gt-1712345.csv --gt runs/phone/gt-1712399.csv   # real logs, no synth
//   npm run eval -- --gt runs/footage/<clip>/tracks.csv --baseline-a runs/footage/<clip>/baseline_a.json --out runs/footage/<clip>/eval
import { parseArgs } from 'node:util';
import { writeStandard } from './synth.ts';
import { loadInputs, synthInputs, fidelitySweep, resilienceSweep, FIDELITY_COLUMNS, RESILIENCE_COLUMNS } from './sweep.ts';
import { writeBaselines } from './baselines.ts';
import { renderAll } from './charts.ts';
import { writeCsv } from './csv.ts';
import { EVAL_DIR, SYNTH_DIR, userPath } from './paths.ts';
import { join } from 'node:path';

const { values } = parseArgs({
  options: {
    gt: { type: 'string', multiple: true },
    duration: { type: 'string', default: '120' },
    hz: { type: 'string', default: '30' },
    seed: { type: 'string', default: '1' },
    out: { type: 'string' },
    'baseline-a': { type: 'string' },
  },
});
const out = values.out ? userPath(values.out) : EVAL_DIR;
const t0 = performance.now();
const lap = (what: string) => console.log(`[${((performance.now() - t0) / 1000).toFixed(1).padStart(5)} s] ${what}`);

let paths: string[];
if (values.gt) {
  paths = values.gt.map(userPath);
  lap(`using ${paths.length} ground-truth log(s)`);
} else {
  writeStandard(SYNTH_DIR, { durationS: Number(values.duration), hz: Number(values.hz), seed: Number(values.seed) });
  paths = synthInputs(SYNTH_DIR);
  lap(`synth: ${paths.length} scenarios -> ${SYNTH_DIR}`);
}
const inputs = loadInputs(paths);
const fidelity = fidelitySweep(inputs);
writeCsv(join(out, 'fidelity_vs_bytes.csv'), FIDELITY_COLUMNS, fidelity);
lap(`sweep: ${fidelity.length} theta points -> fidelity_vs_bytes.csv`);
const resilience = resilienceSweep(inputs);
writeCsv(join(out, 'resilience.csv'), RESILIENCE_COLUMNS, resilience);
lap(`sweep: ${resilience.length} loss points -> resilience.csv`);
writeBaselines(inputs, out, values['baseline-a'] ? userPath(values['baseline-a']) : undefined);
lap('baselines -> baselines.csv');
for (const f of renderAll(out)) lap(`charts -> ${f}`);
