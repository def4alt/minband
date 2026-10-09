// Where inputs and outputs live. runs/ is at the repo root and gitignored.
import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';

export const REPO = fileURLToPath(new URL('../../../', import.meta.url));
export const RUNS = process.env.MINBAND_RUNS ? resolve(process.env.MINBAND_RUNS) : join(REPO, 'runs');
export const SYNTH_DIR = join(RUNS, 'synth');
export const EVAL_DIR = join(RUNS, 'eval');
/** Optional measured Baseline A numbers (see tools/eval/README.md). */
export const BASELINE_A_FILE = join(RUNS, 'baseline_a.json');

/** Resolve a user-supplied path against the directory `npm run` was invoked from. */
export function userPath(p: string): string {
  return resolve(process.env.INIT_CWD ?? process.cwd(), p);
}

/** True when this module is the process entry point (`node src/x.ts`). */
export function isMain(metaUrl: string): boolean {
  return !!process.argv[1] && resolve(process.argv[1]) === fileURLToPath(metaUrl);
}
