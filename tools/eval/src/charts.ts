// Hand-written, dependency-free SVG charts and the summary table, from the sweep CSVs.
//   npm run charts            # reads runs/eval/{fidelity_vs_bytes,resilience,baselines}.csv
// Writes runs/eval/fidelity_vs_bytes.svg, runs/eval/resilience.svg and runs/eval/summary.md.
//
// Style: docs/STYLE.md, dark only. Near-black ground, hairline axes in --ink-3, faint grid in
// --ink-4, uppercase letter-spaced labels, tabular numerals, no frame. Series are told apart by
// colour ("Charts: colour for series"): one muted hue per scenario, fixed by scenario name so it
// is the same in every chart; traces stay 1 px. Reference lines are dashed in --ink-2.
// Presentation attributes carry the whole theme, so any SVG renderer gets the same chart.
// Every marker carries a <title> so browsers show a tooltip on hover.
import { parseArgs } from 'node:util';
import { writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { readCsv } from './csv.ts';
import { CONFIGURED_SOURCE } from './baselines.ts';
import { DEFAULT_THETA } from './sweep.ts';
import { EVAL_DIR, isMain, userPath } from './paths.ts';

type Rec = Record<string, string | number>;

// ---------------------------------------------------------------- theme

const C = { bg: '#070809', ink: '#e9ecef', ink2: '#9aa3ad', ink3: '#4a525b', ink4: '#1a1e23' } as const;
type Tone = 'ink' | 'ink2' | 'ink3';
/** Muted categorical palette for the dark ground: blue, red, green, yellow, purple, then cyan, orange. */
const PALETTE = ['#8ab4f8', '#f28b82', '#81c995', '#fdd663', '#c58af9', '#78d9ec', '#fcad70'];
/** The synthetic scenarios own fixed slots so a scenario keeps its hue in every chart. */
const FIXED = ['static', 'one_walker', 'one_walker_noisy', 'three_walkers', 'crowd'];
function huesFor(scen: string[]): Map<string, string> {
  const m = new Map<string, string>(), used = new Set<number>();
  for (const s of scen) { const k = FIXED.indexOf(s); if (k >= 0) { m.set(s, PALETTE[k]); used.add(k); } }
  let next = 0;
  for (const s of scen) if (!m.has(s)) {
    while (used.has(next % PALETTE.length) && used.size < PALETTE.length) next++;
    used.add(next % PALETTE.length); m.set(s, PALETTE[next % PALETTE.length]); next++;
  }
  return m;
}
const SANS = 'Inter, -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif';
const MONO = '"IBM Plex Mono", ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace';

function style(): string {
  return `<style>svg,text{font-family:${SANS}}.num{font-family:${MONO};font-variant-numeric:tabular-nums}</style>`;
}

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const n = (v: number) => Number(v.toFixed(2));

interface TextOpts { tone?: Tone; size?: number; anchor?: 'start' | 'middle' | 'end'; weight?: number; transform?: string; caps?: boolean; mono?: boolean; spacing?: string }
/** caps = uppercase with 0.18em tracking (labels); mono = tabular numerals (values). */
function text(x: number, y: number, s: string, o: TextOpts = {}): string {
  const attrs = [`x="${n(x)}"`, `y="${n(y)}"`, `fill="${C[o.tone ?? 'ink2']}"`, `font-size="${o.size ?? 11}"`];
  if (o.mono) attrs.push('class="num"', `font-family='${MONO}'`);
  if (o.anchor) attrs.push(`text-anchor="${o.anchor}"`);
  attrs.push(`font-weight="${o.weight ?? (o.mono ? 400 : 300)}"`);
  const spacing = o.spacing ?? (o.caps ? '0.18em' : undefined);
  if (spacing) attrs.push(`letter-spacing="${spacing}"`);
  if (o.transform) attrs.push(`transform="${o.transform}"`);
  return `<text ${attrs.join(' ')}>${esc(o.caps ? s.toUpperCase() : s)}</text>`;
}

function line(x1: number, y1: number, x2: number, y2: number, stroke: string, dash?: string): string {
  return `<line x1="${n(x1)}" y1="${n(y1)}" x2="${n(x2)}" y2="${n(y2)}" stroke="${stroke}" stroke-width="1"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`;
}

/** A 1 px trace in the scenario's hue; `dash` marks a secondary variant (repair off), not a series. */
function path(pts: [number, number][], color: string, dash?: string): string {
  if (pts.length < 2) return '';
  const d = pts.map((p, k) => `${k ? 'L' : 'M'}${n(p[0])},${n(p[1])}`).join('');
  return `<path d="${d}" stroke="${color}" stroke-width="1" fill="none" stroke-linejoin="round"${dash ? ` stroke-dasharray="${dash}" stroke-opacity="0.75"` : ''}/>`;
}

/** Sample point: a small dot in the series hue (hollow for a secondary variant), with a hover tooltip. */
function marker(x: number, y: number, tip: string, o: { color: string; r?: number; hollow?: boolean }): string {
  const r = o.r ?? 2.2;
  const dot = o.hollow
    ? `<circle cx="${n(x)}" cy="${n(y)}" r="${r}" fill="${C.bg}" stroke="${o.color}" stroke-width="1"/>`
    : `<circle cx="${n(x)}" cy="${n(y)}" r="${r}" fill="${o.color}"/>`;
  return `<g><title>${esc(tip)}</title><circle cx="${n(x)}" cy="${n(y)}" r="9" fill="#000" fill-opacity="0" pointer-events="all"/>${dot}</g>`;
}

function svgOpen(w: number, h: number, title: string, desc: string): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-labelledby="t d">`
    + `<title id="t">${esc(title)}</title><desc id="d">${esc(desc)}</desc>${style()}`
    + `<rect fill="${C.bg}" x="0" y="0" width="${w}" height="${h}"/>`;
}

/** Title in the poster register: thin, uppercase, wide tracking. */
const title = (x: number, y: number, s: string) => text(x, y, s, { tone: 'ink', size: 17, weight: 200, caps: true, spacing: '0.3em' });

/** Legend row: key glyph then an uppercase label; wraps at maxX. */
function legend(x0: number, y: number, maxX: number, items: { label: string; key: string }[]): { svg: string; bottom: number } {
  let x = x0, yy = y;
  const out: string[] = [];
  for (const it of items) {
    const w = 52 + it.label.length * 7.6 + 28;
    if (x + w > maxX) { x = x0; yy += 22; }
    out.push(`<g transform="translate(${n(x)},${n(yy)})">${it.key}${text(52, 3.5, it.label, { tone: 'ink2', size: 9.5, caps: true })}</g>`);
    x += w;
  }
  return { svg: out.join(''), bottom: yy + 10 };
}

/** Scenario key: a short trace and dot in the scenario's hue. */
const seriesKey = (color: string) => `<line x1="0" y1="0" x2="40" y2="0" stroke="${color}" stroke-width="1"/><circle cx="20" cy="0" r="2.4" fill="${color}"/>`;
const refKey = (stroke: string, dash?: string) => `<line x1="0" y1="0" x2="40" y2="0" stroke="${stroke}" stroke-width="1"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`;
// Reference lines: dashed in --ink-2; H.264 long dashes, naive metadata short ones.
const REF_H264 = { stroke: C.ink2, dash: '5 4' }, REF_NAIVE = { stroke: C.ink2, dash: '1.5 3' };

/** Nice upper bound and step for a linear axis starting at 0. */
function niceMax(max: number, ticks = 6): { max: number; step: number } {
  if (!(max > 0)) return { max: 1, step: 0.2 };
  const raw = max / ticks, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) ?? 10 * mag;
  return { max: Math.ceil(max / step) * step, step };
}

const fmtBytes = (b: number) => (b >= 1e6 ? `${b / 1e6} MB/s` : b >= 1e3 ? `${b / 1e3} kB/s` : `${b} B/s`);
const fmtBits = (bps: number) => (bps >= 1e6 ? `${Number((bps / 1e6).toPrecision(3))} Mbps` : `${Number((bps / 1e3).toPrecision(3))} kbps`);
const pretty = (s: string) => s.replace(/_/g, ' ');

function scenarioOrder(rows: Rec[]): string[] {
  const seen: string[] = [];
  for (const r of rows) if (!seen.includes(String(r.scenario))) seen.push(String(r.scenario));
  return seen;
}

// ---------------------------------------------------------------- fidelity vs bytes

export function fidelityChart(rows: Rec[], baselines: Rec[], meta: { note?: string } = {}): string {
  const W = 960, H = 600, X0 = 40;
  const scen = scenarioOrder(rows), hue = huesFor(scen);
  const A = baselines.filter(b => b.kind === 'A');
  const B = baselines.filter(b => b.kind === 'B');
  const allMeasured = A.length > 0 && A.every(a => a.measured === 'true');
  const noneMeasured = !A.some(a => a.measured === 'true');

  const head: string[] = [];
  head.push(title(X0, 46, 'Twin error vs bandwidth'));
  head.push(text(X0, 70, 'Error falls as bytes rise. Each line sweeps θ_pos from 2.0 m (left) to 0.02 m (right), θ_vel = 2·θ_pos, no loss.', { tone: 'ink2', size: 11.5 }));
  const leg = legend(X0, 100, W - X0, [
    ...scen.map(s => ({ label: pretty(s), key: seriesKey(hue.get(s)!) })),
    { label: `θ_pos ${DEFAULT_THETA} m default`, key: `<circle cx="20" cy="0" r="5" fill="none" stroke="${C.ink2}" stroke-width="1"/><circle cx="20" cy="0" r="2.4" fill="${C.ink}"/>` },
    { label: allMeasured ? 'H.264 measured' : noneMeasured ? 'H.264 configured' : 'H.264 video', key: refKey(REF_H264.stroke, REF_H264.dash) },
    { label: 'naive 30 Hz metadata', key: refKey(REF_NAIVE.stroke, REF_NAIVE.dash) },
  ]);
  head.push(leg.svg);

  const L = 84, R = W - X0, T = leg.bottom + 30, Bm = H - 92;
  const xs = [...rows.map(r => Number(r.bytes_per_s)), ...A.map(a => Number(a.bytes_per_s)), ...B.map(b => Number(b.bytes_per_s))].filter(v => v > 0);
  const lx0 = Math.floor(Math.log10(Math.min(10, ...xs)));
  const lx1 = Math.ceil(Math.log10(Math.max(...xs) * 1.15) * 2) / 2;
  const X = (b: number) => L + ((Math.log10(Math.max(b, 10 ** lx0)) - lx0) / (lx1 - lx0)) * (R - L);
  const yMax = niceMax(Math.max(...rows.map(r => Number(r.err_mean_m) * 100)) * 1.05);
  const Y = (cm: number) => Bm - (cm / yMax.max) * (Bm - T);

  const g: string[] = [];
  // grid + y axis
  for (let v = 0; v <= yMax.max + 1e-9; v += yMax.step) {
    const y = Y(v);
    g.push(line(L, y, R, y, v === 0 ? C.ink3 : C.ink4));
    g.push(text(L - 10, y + 3.5, `${Number(v.toFixed(1))}`, { tone: 'ink2', size: 10, anchor: 'end', mono: true }));
  }
  g.push(line(L, T, L, Bm, C.ink3));
  g.push(text(22, (T + Bm) / 2, 'Mean twin error, cm', { tone: 'ink2', size: 9.5, anchor: 'middle', caps: true, transform: `rotate(-90 22 ${n((T + Bm) / 2)})` }));
  // x axis: decades with gridlines, minor ticks
  for (let d = lx0; d <= Math.floor(lx1); d++) {
    const x = X(10 ** d);
    g.push(line(x, T, x, Bm, C.ink4));
    g.push(text(x, Bm + 20, fmtBytes(10 ** d), { tone: 'ink2', size: 10, anchor: 'middle', mono: true }));
    for (let m = 2; m <= 9; m++) { const v = m * 10 ** d; if (Math.log10(v) <= lx1) { const xm = X(v); g.push(line(xm, Bm, xm, Bm + 4, C.ink3)); } }
  }
  g.push(text((L + R) / 2, Bm + 46, 'Uplink bytes per second, log scale · payload + 28 B UDP/IP per datagram', { tone: 'ink2', size: 9.5, anchor: 'middle', caps: true }));

  // reference lines: H.264 and naive metadata, grouped when values coincide
  const refs: { x: number; label: string; ref: { stroke: string; dash: string } }[] = [];
  for (const a of A) refs.push({ x: Number(a.bytes_per_s), label: `${a.label} · ${fmtBits(Number(a.bps))}${a.measured === 'true' ? '' : ' (configured)'}`, ref: REF_H264 });
  const bGroups = new Map<string, { bytes: number; bps: number; ent: number }>();
  for (const b of B) { const k = Number(b.entities_mean).toFixed(2); if (!bGroups.has(k)) bGroups.set(k, { bytes: Number(b.bytes_per_s), bps: Number(b.bps), ent: Number(b.entities_mean) }); }
  for (const b of bGroups.values()) {
    const ent = Number.isInteger(b.ent) ? String(b.ent) : b.ent.toFixed(1);
    refs.push({ x: b.bytes, label: `naive 30 Hz · ${ent} ${b.ent === 1 ? 'entity' : 'entities'} · ${fmtBits(b.bps)}`, ref: REF_NAIVE });
  }
  for (const r of refs) {
    const x = X(r.x);
    g.push(line(x, T, x, Bm, r.ref.stroke, r.ref.dash));
    g.push(text(x - 5, T + 4, r.label, { tone: 'ink2', size: 9.5, anchor: 'end', mono: true, transform: `rotate(-90 ${n(x - 5)} ${n(T + 4)})` }));
  }

  // series
  scen.forEach(s => {
    const color = hue.get(s)!;
    const pts = rows.filter(r => r.scenario === s).sort((a, b) => Number(a.bytes_per_s) - Number(b.bytes_per_s) || Number(b.theta_pos) - Number(a.theta_pos));
    g.push(path(pts.map(p => [X(Number(p.bytes_per_s)), Y(Number(p.err_mean_m) * 100)]), color));
    for (const p of pts) {
      const isDef = Math.abs(Number(p.theta_pos) - DEFAULT_THETA) < 1e-9;
      const tip = `${pretty(s)} · θ_pos ${p.theta_pos} m / θ_vel ${p.theta_vel} m/s\n${Number(p.bytes_per_s).toFixed(1)} B/s (${Number(p.kbps).toFixed(2)} kbps), ${p.deltas} deltas + ${p.keyframes} keyframes\nmean ${(Number(p.err_mean_m) * 100).toFixed(2)} cm · p95 ${(Number(p.err_p95_m) * 100).toFixed(2)} cm · max ${(Number(p.err_max_m) * 100).toFixed(2)} cm`;
      if (!isDef) g.push(marker(X(Number(p.bytes_per_s)), Y(Number(p.err_mean_m) * 100), tip, { color }));
    }
    const d = pts.find(p => Math.abs(Number(p.theta_pos) - DEFAULT_THETA) < 1e-9);
    if (d) {
      const x = X(Number(d.bytes_per_s)), y = Y(Number(d.err_mean_m) * 100);
      g.push(`<circle cx="${n(x)}" cy="${n(y)}" r="5" fill="none" stroke="${C.ink2}" stroke-width="1"/>`);
      g.push(marker(x, y, `${pretty(s)} · DEFAULT θ_pos ${d.theta_pos} m\n${Number(d.bytes_per_s).toFixed(1)} B/s (${Number(d.kbps).toFixed(2)} kbps)\nmean ${(Number(d.err_mean_m) * 100).toFixed(2)} cm · p95 ${(Number(d.err_p95_m) * 100).toFixed(2)} cm`, { color, r: 2.4 }));
    }
  });

  const foot = text(X0, H - 22, meta.note ?? `Baseline A ${allMeasured ? 'measured on the phone' : noneMeasured ? `is ${CONFIGURED_SOURCE}` : `marked (configured) is ${CONFIGURED_SOURCE}`}. Baseline B = entities × 31 B × 30 Hz + 30 Hz × 40 B. Table: summary.md.`, { tone: 'ink2', size: 10 });
  return svgOpen(W, H, 'Twin error vs bandwidth', 'Mean twin position error against uplink bytes per second for each scenario (one colour each) as the position threshold is swept; vertical dashed reference lines mark H.264 video and naive 30 Hz metadata.')
    + head.join('') + g.join('') + foot + '</svg>\n';
}

// ---------------------------------------------------------------- resilience

export function resilienceChart(rows: Rec[], meta: { note?: string } = {}): string {
  const W = 960, H = 560, X0 = 40;
  const scen = scenarioOrder(rows), hue = huesFor(scen);
  const losses = [...new Set(rows.map(r => Number(r.loss)))].sort((a, b) => a - b);
  const theta = rows.length ? Number(rows[0].theta_pos) : DEFAULT_THETA;
  const delay = rows.length ? Number(rows[0].delay_ticks) : 0;
  const seeds = Math.max(...rows.map(r => Number(r.seeds)), 1);

  const head: string[] = [];
  head.push(title(X0, 46, 'Twin under packet loss'));
  head.push(text(X0, 70, `θ_pos ${theta} m, θ_vel ${2 * theta} m/s, ${delay} ticks (${(delay / 120 * 1000).toFixed(0)} ms) one-way delay, Bernoulli loss both directions, mean of ${seeds} seeds.`, { tone: 'ink2', size: 11.5 }));
  const leg = legend(X0, 100, W - X0, [
    ...scen.map(s => ({ label: pretty(s), key: seriesKey(hue.get(s)!) })),
    { label: 'state repair on (nack → resend)', key: `<line x1="0" y1="0" x2="40" y2="0" stroke="${C.ink2}" stroke-width="1"/><circle cx="20" cy="0" r="2.2" fill="${C.ink2}"/>` },
    { label: 'repair off (keyframes only)', key: `<line x1="0" y1="0" x2="40" y2="0" stroke="${C.ink2}" stroke-width="1" stroke-dasharray="4 3"/><circle cx="20" cy="0" r="2.2" fill="${C.bg}" stroke="${C.ink2}" stroke-width="1"/>` },
  ]);
  head.push(leg.svg);

  const T = leg.bottom + 44, Bm = H - 96;
  const panels = [
    { L: 84, R: 452, title: 'Mean twin error, entities present, cm', val: (r: Rec) => Number(r.err_mean_present_m) * 100 },
    { L: 568, R: 920, title: 'Availability, % of ground-truth rows in the twin', val: (r: Rec) => Number(r.availability) * 100 },
  ];
  const g: string[] = [];
  panels.forEach((p, pi) => {
    const lossMax = Math.max(...losses, 0.05);
    const X = (l: number) => p.L + (l / lossMax) * (p.R - p.L);
    const vals = rows.map(p.val);
    let y0 = 0, y1: number, step: number;
    if (pi === 0) { const m = niceMax(Math.max(...vals) * 1.05); y1 = m.max; step = m.step; } else {
      const lo = Math.min(...vals);
      const span = Math.max(100 - lo, 0.5);
      const m = niceMax(span * 1.1, 5);
      step = m.step; y1 = 100; y0 = 100 - Math.ceil((span * 1.1) / step) * step;
    }
    const Y = (v: number) => Bm - ((v - y0) / (y1 - y0)) * (Bm - T);
    g.push(text(p.L, T - 18, p.title, { tone: 'ink', size: 9.5, caps: true, weight: 400 }));
    for (let v = y0; v <= y1 + 1e-9; v += step) {
      const y = Y(v);
      g.push(line(p.L, y, p.R, y, v === y0 ? C.ink3 : C.ink4));
      g.push(text(p.L - 10, y + 3.5, `${Number(v.toFixed(2))}${pi === 1 ? '%' : ''}`, { tone: 'ink2', size: 10, anchor: 'end', mono: true }));
    }
    g.push(line(p.L, T, p.L, Bm, C.ink3));
    for (const l of losses) {
      const x = X(l);
      if (l > 0) g.push(line(x, T, x, Bm, C.ink4));
      g.push(text(x, Bm + 20, `${Number((l * 100).toFixed(1))}%`, { tone: 'ink2', size: 10, anchor: 'middle', mono: true }));
    }
    g.push(text((p.L + p.R) / 2, Bm + 44, 'Packet loss rate', { tone: 'ink2', size: 9.5, anchor: 'middle', caps: true }));
    scen.forEach(s => {
      const color = hue.get(s)!;
      for (const repair of ['off', 'on']) {
        const pts = rows.filter(r => r.scenario === s && r.repair === repair).sort((a, b) => Number(a.loss) - Number(b.loss));
        g.push(path(pts.map(r => [X(Number(r.loss)), Y(p.val(r))]), color, repair === 'off' ? '4 3' : undefined));
        for (const r of pts) {
          const tip = `${pretty(s)} · loss ${(Number(r.loss) * 100).toFixed(0)}% · repair ${repair}\nmean error ${(Number(r.err_mean_present_m) * 100).toFixed(2)} cm (present) · p95 ${(Number(r.err_p95_present_m) * 100).toFixed(2)} cm\navailability ${(Number(r.availability) * 100).toFixed(2)}% · ${Number(r.bytes_per_s).toFixed(1)} B/s · phantom up to ${Number(r.phantom_max_s).toFixed(1)} s`;
          g.push(marker(X(Number(r.loss)), Y(p.val(r)), tip, { color, hollow: repair === 'off' }));
        }
      }
    });
  });
  const foot = text(X0, H - 22, meta.note ?? 'Error is measured at every logged frame against the ground-truth track; a missing entity counts towards availability, not error. Table: summary.md.', { tone: 'ink2', size: 10 });
  return svgOpen(W, H, 'Twin under packet loss', 'Two panels against packet loss rate: mean twin error of present entities, and availability, per scenario (one colour each), with state repair on (solid trace, filled dots) and off (dashed trace, hollow dots).')
    + head.join('') + g.join('') + foot + '</svg>\n';
}

// ---------------------------------------------------------------- summary.md

const fx = (v: number, d = 1) => (Number.isFinite(v) ? v.toFixed(d) : '-');
const times = (a: number, b: number) => (b > 0 ? `${Math.round(a / b).toLocaleString('en-US')}×` : '-');

export function summary(fid: Rec[], res: Rec[], base: Rec[], meta: { title?: string; source?: string } = {}): string {
  const A = base.filter(b => b.kind === 'A');
  const B = new Map(base.filter(b => b.kind === 'B').map(b => [String(b.scenario), b]));
  const at = fid.filter(r => Math.abs(Number(r.theta_pos) - DEFAULT_THETA) < 1e-9);
  const scen = scenarioOrder(fid);
  const lines: string[] = [];
  lines.push(`# ${meta.title ?? 'MinBand evaluation summary'}`, '');
  lines.push(meta.source ?? 'Ground truth: synthetic scenarios from `tools/eval` (runs/synth). Replayed in-process through the WASM Edge and Receiver.', '');
  const d0 = at[0];
  if (d0) lines.push(`Operating point: θ_pos ${DEFAULT_THETA} m, θ_vel ${2 * DEFAULT_THETA} m/s, no loss, no delay; ${fx(Number(d0.duration_s), 0)} s per log at ${d0.frame_hz} Hz. Bytes are on the wire: payload + 28 B UDP/IP per datagram.`, '');
  lines.push('## Key numbers', '');
  const aCols = A.map(a => `vs ${a.label}${a.measured === 'true' ? '' : '*'}`);
  lines.push(`| Scenario | Entities | MinBand B/s | MinBand kbps | Datagrams (Δ / kf) | Mean error (cm) | p95 error (cm) | Naive 30 Hz kbps | vs naive | ${aCols.join(' | ')} |`);
  lines.push(`|---|---:|---:|---:|---:|---:|---:|---:|---:|${aCols.map(() => '---:').join('|')}|`);
  for (const s of scen) {
    const r = at.find(x => x.scenario === s);
    if (!r) continue;
    const bps = Number(r.bytes_per_s) * 8, b = B.get(s);
    const nb = b ? Number(b.bps) : NaN;
    lines.push(`| ${pretty(s)} | ${fx(Number(r.entities_mean), Number.isInteger(Number(r.entities_mean)) ? 0 : 1)} | ${fx(Number(r.bytes_per_s))} | ${fx(Number(r.kbps), 2)} | ${r.datagrams} (${r.deltas} / ${r.keyframes}) | ${fx(Number(r.err_mean_m) * 100, 2)} | ${fx(Number(r.err_p95_m) * 100, 2)} | ${fx(nb / 1000, 2)} | ${times(nb, bps)} | ${A.map(a => times(Number(a.bps), bps)).join(' | ')} |`);
  }
  lines.push('');
  if (A.some(a => a.measured !== 'true')) lines.push(`\\* Baseline A marked * is ${CONFIGURED_SOURCE}.`, '');
  lines.push('## Baselines', '');
  lines.push('| Baseline | Bitrate | Source |', '|---|---:|---|');
  for (const a of A) lines.push(`| A: ${a.label} (${a.resolution}) | ${fmtBits(Number(a.bps))} | ${a.measured === 'true' ? a.source : CONFIGURED_SOURCE} |`);
  for (const s of scen) { const b = B.get(s); if (b) lines.push(`| B: naive 30 Hz, ${pretty(s)} (${fx(Number(b.entities_mean), 2)} entities) | ${fmtBits(Number(b.bps))} | entities × 31 B × 30 Hz × 8 + 30 Hz × 40 B × 8 |`); }
  lines.push('');
  if (res.length) {
    const losses = [...new Set(res.map(r => Number(r.loss)))].sort((a, b) => a - b);
    const r0 = res[0];
    lines.push('## Resilience', '');
    lines.push(`θ_pos ${r0.theta_pos} m, ${r0.delay_ticks} ticks one-way delay, loss in both directions, mean of ${Math.max(...res.map(r => Number(r.seeds)))} seeds. Cell: mean error of present entities (cm) / availability, repair on → repair off.`, '');
    lines.push(`| Scenario | ${losses.map(l => `loss ${Number((l * 100).toFixed(1))}%`).join(' | ')} |`);
    lines.push(`|---|${losses.map(() => '---').join('|')}|`);
    for (const s of scenarioOrder(res)) {
      const cells = losses.map(l => {
        const on = res.find(r => r.scenario === s && Number(r.loss) === l && r.repair === 'on');
        const off = res.find(r => r.scenario === s && Number(r.loss) === l && r.repair === 'off');
        const c = (r?: Rec) => (r ? `${fx(Number(r.err_mean_present_m) * 100)} / ${fx(Number(r.availability) * 100, 1)}%` : '-');
        return l === 0 ? c(on) : `${c(on)} → ${c(off)}`;
      });
      lines.push(`| ${pretty(s)} | ${cells.join(' | ')} |`);
    }
    lines.push('');
    if (res.some(r => r.repair === 'server')) {
      lines.push('Uplink bytes/s and mean error (present, cm) by ack policy: repair on (acks only when a gap needs a nack) → the live server\'s cadence (ack after any datagram when 100 ms have passed; every ack re-lists all open gaps, so each lost seq is repaired repeatedly).', '');
      lines.push(`| Scenario | ${losses.filter(l => l > 0).map(l => `loss ${Number((l * 100).toFixed(1))}%`).join(' | ')} |`);
      lines.push(`|---|${losses.filter(l => l > 0).map(() => '---').join('|')}|`);
      for (const s of scenarioOrder(res)) {
        const cells = losses.filter(l => l > 0).map(l => {
          const on = res.find(r => r.scenario === s && Number(r.loss) === l && r.repair === 'on');
          const sv = res.find(r => r.scenario === s && Number(r.loss) === l && r.repair === 'server');
          const c = (r?: Rec) => (r ? `${fx(Number(r.bytes_per_s), 0)} B/s, ${fx(Number(r.err_mean_present_m) * 100)} cm` : '-');
          return `${c(on)} → ${c(sv)}`;
        });
        lines.push(`| ${pretty(s)} | ${cells.join(' | ')} |`);
      }
      lines.push('');
    }
  }
  lines.push('Charts: `fidelity_vs_bytes.svg`, `resilience.svg`. Raw data: `fidelity_vs_bytes.csv`, `resilience.csv`, `baselines.csv` (columns in tools/README.md).', '');
  return lines.join('\n');
}

export function renderAll(dir = EVAL_DIR): string[] {
  const need = ['fidelity_vs_bytes.csv', 'resilience.csv', 'baselines.csv'].map(f => join(dir, f));
  for (const f of need) if (!existsSync(f)) throw new Error(`missing ${f}; run \`npm run sweep\` and \`npm run baselines\` first`);
  const [fid, res, base] = need.map(readCsv);
  mkdirSync(dir, { recursive: true });
  const out = [join(dir, 'fidelity_vs_bytes.svg'), join(dir, 'resilience.svg'), join(dir, 'summary.md')];
  writeFileSync(out[0], fidelityChart(fid, base));
  writeFileSync(out[1], resilienceChart(res));
  writeFileSync(out[2], summary(fid, res, base));
  return out;
}

if (isMain(import.meta.url)) {
  const { values } = parseArgs({ options: { dir: { type: 'string' } } });
  for (const f of renderAll(values.dir ? userPath(values.dir) : EVAL_DIR)) console.log(`charts -> ${f}`);
}
