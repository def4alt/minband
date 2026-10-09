// Deterministic seeded RNG (mulberry32) plus a Box-Muller gaussian. Used for synthetic scenes,
// observation noise and link loss so every run is reproducible from its seed.

export interface Rng {
  /** Uniform in [0, 1). */
  next(): number;
  /** Standard normal sample. */
  gauss(): number;
  /** Uniform in [lo, hi). */
  range(lo: number, hi: number): number;
}

export function rng(seed: number): Rng {
  let a = (seed >>> 0) || 0x9e3779b9;
  let spare: number | null = null;
  const next = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const gauss = () => {
    if (spare !== null) { const s = spare; spare = null; return s; }
    let u = 0;
    while (u === 0) u = next();
    const v = next();
    const r = Math.sqrt(-2 * Math.log(u));
    spare = r * Math.sin(2 * Math.PI * v);
    return r * Math.cos(2 * Math.PI * v);
  };
  return { next, gauss, range: (lo, hi) => lo + (hi - lo) * next() };
}

/** Derive an independent stream seed from a base seed and a label. */
export function subSeed(seed: number, label: string): number {
  let h = (seed ^ 0x811c9dc5) >>> 0;
  for (let i = 0; i < label.length; i++) h = Math.imul(h ^ label.charCodeAt(i), 0x01000193) >>> 0;
  return h;
}
