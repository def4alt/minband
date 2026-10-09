// Hand-written, dependency-free SVG charts and the summary table, from the sweep CSVs.
//   npm run charts            # reads runs/eval/{fidelity_vs_bytes,resilience,baselines}.csv
// Writes runs/eval/fidelity_vs_bytes.svg, runs/eval/resilience.svg and runs/eval/summary.md.
//
// Colours: presentation attributes carry the light theme (so renderers without CSS still get a
// legible chart on its own light surface); a <style> block re-skins every element for
// prefers-color-scheme: dark. Palette: validated categorical order (blue, orange, aqua, yellow,
// magenta, ...), separate light and dark steps; neutral grey axes; hairline solid gridlines.
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

const LIGHT = { bg: '#fcfcfb', ink: '#0b0b0b', ink2: '#52514e', muted: '#898781', grid: '#e1e0d9', axis: '#c3c2b7', ref: '#898781' };
const DARK = { bg: '#1a1a19', ink: '#ffffff', ink2: '#c3c2b7', muted: '#898781', grid: '#2c2c2a', axis: '#383835', ref: '#898781' };
const SERIES_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'];
const SERIES_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];
const FONT = 'system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif';

function style(): string {
  const role = (t: typeof LIGHT, s: string[]) => [
    `.bg{fill:${t.bg}}`, `.ink{fill:${t.ink}}`, `.ink2{fill:${t.ink2}}`, `.muted{fill:${t.muted}}`,
    `.grid{stroke:${t.grid}}`, `.axis{stroke:${t.axis}}`, `.ref{stroke:${t.ref}}`, `.ring{stroke:${t.bg}}`, `.hollow{fill:${t.bg}}`, `.halo{stroke:${t.ink2}}`,
    ...s.map((c, i) => `.s${i}{stroke:${c}}.f${i}{fill:${c}}`),
  ].join('');
  return `<style>svg{font-family:${FONT}}text{font-family:${FONT}}`
    + `@media (prefers-color-scheme: dark){${role(DARK, SERIES_DARK)}}</style>`;
}

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const n = (v: number) => Number(v.toFixed(2));

function text(x: number, y: number, s: string, o: { cls?: 'ink' | 'ink2' | 'muted'; size?: number; anchor?: 'start' | 'middle' | 'end'; weight?: number; transform?: string; baseline?: string } = {}): string {
  const cls = o.cls ?? 'ink2';
  const fill = LIGHT[cls];
  const attrs = [`x="${n(x)}"`, `y="${n(y)}"`, `class="${cls}"`, `fill="${fill}"`, `font-size="${o.size ?? 12}"`];
  if (o.anchor) attrs.push(`text-anchor="${o.anchor}"`);
  if (o.weight) attrs.push(`font-weight="${o.weight}"`);
  if (o.baseline) attrs.push(`dominant-baseline="${o.baseline}"`);
  if (o.transform) attrs.push(`transform="${o.transform}"`);
  return `<text ${attrs.join(' ')}>${esc(s)}</text>`;
}

function line(x1: number, y1: number, x2: number, y2: number, cls: string, stroke: string, width = 1, dash?: string): string {
  return `<line x1="${n(x1)}" y1="${n(y1)}" x2="${n(x2)}" y2="${n(y2)}" class="${cls}" stroke="${stroke}" stroke-width="${width}"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`;
}

function path(pts: [number, number][], i: number, dash?: string): string {
  if (pts.length < 2) return '';
  const d = pts.map((p, k) => `${k ? 'L' : 'M'}${n(p[0])},${n(p[1])}`).join('');
  return `<path d="${d}" class="s${i % 8}" stroke="${SERIES_LIGHT[i % 8]}" stroke-width="2" fill="none" stroke-linejoin="round" stroke-linecap="round"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`;
}

function marker(x: number, y: number, i: number, tip: string, r = 4, hollow = false): string {
  const c = SERIES_LIGHT[i % 8];
  const dot = hollow
    ? `<circle cx="${n(x)}" cy="${n(y)}" r="${r}" class="hollow s${i % 8}" fill="${LIGHT.bg}" stroke="${c}" stroke-width="2"/>`
    : `<circle cx="${n(x)}" cy="${n(y)}" r="${r}" class="f${i % 8} ring" fill="${c}" stroke="${LIGHT.bg}" stroke-width="2"/>`;
  return `<g><title>${esc(tip)}</title><circle cx="${n(x)}" cy="${n(y)}" r="${Math.max(10, r + 6)}" fill="#000" fill-opacity="0" pointer-events="all"/>${dot}</g>`;
}

function svgOpen(w: number, h: number, title: string, desc: string): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-labelledby="t d">`
    + `<title id="t">${esc(title)}</title><desc id="d">${esc(desc)}</desc>${style()}`
    + `<rect class="bg" fill="${LIGHT.bg}" x="0" y="0" width="${w}" height="${h}" rx="8"/>`;
}

/** Legend row: series keys (line + dot) then extra keys. Returns svg and the row height used. */
function legend(x0: number, y: number, maxX: number, items: { label: string; key: string }[]): { svg: string; bottom: number } {
  let x = x0, yy = y;
  const out: string[] = [];
  for (const it of items) {
    const w = 30 + it.label.length * 6.6 + 18;
    if (x + w > maxX) { x = x0; yy += 20; }
    out.push(`<g transform="translate(${n(x)},${n(yy)})">${it.key}${text(28, 4, it.label, { cls: 'ink2', size: 12 })}</g>`);
    x += w;
  }
  return { svg: out.join(''), bottom: yy + 10 };
}

const seriesKey = (i: number, dash?: string, hollow = false) =>
  `<line x1="0" y1="0" x2="22" y2="0" class="s${i % 8}" stroke="${SERIES_LIGHT[i % 8]}" stroke-width="2"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`
  + (hollow ? `<circle cx="11" cy="0" r="4" class="hollow s${i % 8}" fill="${LIGHT.bg}" stroke="${SERIES_LIGHT[i % 8]}" stroke-width="2"/>`
    : `<circle cx="11" cy="0" r="4" class="f${i % 8} ring" fill="${SERIES_LIGHT[i % 8]}" stroke="${LIGHT.bg}" stroke-width="2"/>`);
const neutralKey = (cls: 'ref' | 'ink2', dash?: string, w = 1.5) => `<line x1="0" y1="0" x2="22" y2="0" class="${cls}" stroke="${LIGHT.ref}" stroke-width="${w}"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`;

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
  const W = 960, H = 600;
  const scen = scenarioOrder(rows);
  const A = baselines.filter(b => b.kind === 'A');
  const B = baselines.filter(b => b.kind === 'B');
  const allMeasured = A.length > 0 && A.every(a => a.measured === 'true');
  const noneMeasured = !A.some(a => a.measured === 'true');

  const head: string[] = [];
  head.push(text(32, 40, 'Twin error vs bandwidth', { cls: 'ink', size: 20, weight: 600 }));
  head.push(text(32, 62, 'Error falls as bytes rise. Each line sweeps θ_pos from 2.0 m (left) to 0.02 m (right), θ_vel = 2·θ_pos, no loss.', { cls: 'ink2', size: 13 }));
  const leg = legend(32, 90, W - 32, [
    ...scen.map((s, i) => ({ label: pretty(s), key: seriesKey(i) })),
    { label: `θ_pos ${DEFAULT_THETA} m (default)`, key: `<circle cx="11" cy="0" r="6.5" class="hollow halo" fill="${LIGHT.bg}" stroke="${LIGHT.ink2}" stroke-width="2"/>` },
    { label: allMeasured ? 'H.264 (measured)' : noneMeasured ? 'H.264 (configured)' : 'H.264 video', key: neutralKey('ref') },
    { label: 'naive 30 Hz metadata', key: neutralKey('ref', '5 4') },
  ]);
  head.push(leg.svg);

  const L = 76, R = W - 32, T = leg.bottom + 24, Bm = H - 86;
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
    g.push(line(L, y, R, y, v === 0 ? 'axis' : 'grid', v === 0 ? LIGHT.axis : LIGHT.grid));
    g.push(text(L - 8, y + 4, `${Number(v.toFixed(1))}`, { cls: 'muted', size: 12, anchor: 'end' }));
  }
  g.push(text(18, (T + Bm) / 2, 'Mean twin error (cm)', { cls: 'ink2', size: 12, anchor: 'middle', transform: `rotate(-90 18 ${n((T + Bm) / 2)})` }));
  // x axis: decades with gridlines, minor ticks
  for (let d = lx0; d <= Math.floor(lx1); d++) {
    const x = X(10 ** d);
    g.push(line(x, T, x, Bm, 'grid', LIGHT.grid));
    g.push(text(x, Bm + 20, fmtBytes(10 ** d), { cls: 'muted', size: 12, anchor: 'middle' }));
    for (let m = 2; m <= 9; m++) { const v = m * 10 ** d; if (Math.log10(v) <= lx1) { const xm = X(v); g.push(line(xm, Bm, xm, Bm + 4, 'axis', LIGHT.axis)); } }
  }
  g.push(text((L + R) / 2, Bm + 44, 'Uplink bytes per second, log scale (payload + 28 B UDP/IP per datagram)', { cls: 'ink2', size: 12, anchor: 'middle' }));

  // reference lines: H.264 (solid) and naive metadata (dashed), grouped when values coincide
  const refs: { x: number; label: string; dash?: string }[] = [];
  for (const a of A) refs.push({ x: Number(a.bytes_per_s), label: `${a.label} · ${fmtBits(Number(a.bps))}${a.measured === 'true' ? '' : ' (configured)'}` });
  const bGroups = new Map<string, { bytes: number; bps: number; ent: number }>();
  for (const b of B) { const k = Number(b.entities_mean).toFixed(2); if (!bGroups.has(k)) bGroups.set(k, { bytes: Number(b.bytes_per_s), bps: Number(b.bps), ent: Number(b.entities_mean) }); }
  for (const b of bGroups.values()) {
    const ent = Number.isInteger(b.ent) ? String(b.ent) : b.ent.toFixed(1);
    refs.push({ x: b.bytes, label: `naive 30 Hz · ${ent} ${b.ent === 1 ? 'entity' : 'entities'} · ${fmtBits(b.bps)}`, dash: '5 4' });
  }
  for (const r of refs) {
    const x = X(r.x);
    g.push(line(x, T, x, Bm, 'ref', LIGHT.ref, r.dash ? 1 : 1.5, r.dash));
    g.push(text(x - 5, T + 4, r.label, { cls: 'ink2', size: 11, anchor: 'end', transform: `rotate(-90 ${n(x - 5)} ${n(T + 4)})` }));
  }

  // series
  scen.forEach((s, i) => {
    const pts = rows.filter(r => r.scenario === s).sort((a, b) => Number(a.bytes_per_s) - Number(b.bytes_per_s) || Number(b.theta_pos) - Number(a.theta_pos));
    g.push(path(pts.map(p => [X(Number(p.bytes_per_s)), Y(Number(p.err_mean_m) * 100)]), i));
    for (const p of pts) {
      const isDef = Math.abs(Number(p.theta_pos) - DEFAULT_THETA) < 1e-9;
      const tip = `${pretty(s)} · θ_pos ${p.theta_pos} m / θ_vel ${p.theta_vel} m/s\n${Number(p.bytes_per_s).toFixed(1)} B/s (${Number(p.kbps).toFixed(2)} kbps), ${p.deltas} deltas + ${p.keyframes} keyframes\nmean ${(Number(p.err_mean_m) * 100).toFixed(2)} cm · p95 ${(Number(p.err_p95_m) * 100).toFixed(2)} cm · max ${(Number(p.err_max_m) * 100).toFixed(2)} cm`;
      if (!isDef) g.push(marker(X(Number(p.bytes_per_s)), Y(Number(p.err_mean_m) * 100), i, tip));
    }
    const d = pts.find(p => Math.abs(Number(p.theta_pos) - DEFAULT_THETA) < 1e-9);
    if (d) {
      const x = X(Number(d.bytes_per_s)), y = Y(Number(d.err_mean_m) * 100);
      g.push(`<circle cx="${n(x)}" cy="${n(y)}" r="8.5" class="hollow halo" fill="${LIGHT.bg}" stroke="${LIGHT.ink2}" stroke-width="2"/>`);
      g.push(marker(x, y, i, `${pretty(s)} · DEFAULT θ_pos ${d.theta_pos} m\n${Number(d.bytes_per_s).toFixed(1)} B/s (${Number(d.kbps).toFixed(2)} kbps)\nmean ${(Number(d.err_mean_m) * 100).toFixed(2)} cm · p95 ${(Number(d.err_p95_m) * 100).toFixed(2)} cm`, 4.5));
    }
  });

  const foot = text(32, H - 18, meta.note ?? `Baseline A ${allMeasured ? 'measured on the phone' : noneMeasured ? `is ${CONFIGURED_SOURCE}` : `marked (configured) is ${CONFIGURED_SOURCE}`}. Baseline B = entities × 31 B × 30 Hz + 30 Hz × 40 B. Table: summary.md.`, { cls: 'muted', size: 11 });
  return svgOpen(W, H, 'Twin error vs bandwidth', 'Mean twin position error against uplink bytes per second for each scenario as the position threshold is swept; vertical reference lines mark H.264 video and naive 30 Hz metadata.')
    + head.join('') + g.join('') + foot + '</svg>\n';
}

// ---------------------------------------------------------------- resilience

export function resilienceChart(rows: Rec[], meta: { note?: string } = {}): string {
  const W = 960, H = 540;
  const scen = scenarioOrder(rows);
  const losses = [...new Set(rows.map(r => Number(r.loss)))].sort((a, b) => a - b);
  const theta = rows.length ? Number(rows[0].theta_pos) : DEFAULT_THETA;
  const delay = rows.length ? Number(rows[0].delay_ticks) : 0;
  const seeds = Math.max(...rows.map(r => Number(r.seeds)), 1);

  const head: string[] = [];
  head.push(text(32, 40, 'Twin under packet loss', { cls: 'ink', size: 20, weight: 600 }));
  head.push(text(32, 62, `θ_pos ${theta} m, θ_vel ${2 * theta} m/s, ${delay} ticks (${(delay / 120 * 1000).toFixed(0)} ms) one-way delay, Bernoulli loss both directions, mean of ${seeds} seeds.`, { cls: 'ink2', size: 13 }));
  const leg = legend(32, 90, W - 32, [
    ...scen.map((s, i) => ({ label: pretty(s), key: seriesKey(i) })),
    { label: 'state repair on (nack → resend)', key: neutralKey('ink2', undefined, 2) },
    { label: 'repair off (keyframes only)', key: neutralKey('ink2', '6 4', 2) },
  ]);
  head.push(leg.svg);

  const T = leg.bottom + 34, Bm = H - 92;
  const panels = [
    { L: 76, R: 452, title: 'Mean twin error, entities present (cm)', val: (r: Rec) => Number(r.err_mean_present_m) * 100 },
    { L: 556, R: 928, title: 'Availability (% of ground-truth rows in the twin)', val: (r: Rec) => Number(r.availability) * 100 },
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
    g.push(text(p.L, T - 14, p.title, { cls: 'ink', size: 13, weight: 600 }));
    for (let v = y0; v <= y1 + 1e-9; v += step) {
      const y = Y(v);
      g.push(line(p.L, y, p.R, y, v === y0 ? 'axis' : 'grid', v === y0 ? LIGHT.axis : LIGHT.grid));
      g.push(text(p.L - 8, y + 4, `${Number(v.toFixed(2))}${pi === 1 ? '%' : ''}`, { cls: 'muted', size: 12, anchor: 'end' }));
    }
    for (const l of losses) {
      const x = X(l);
      if (l > 0) g.push(line(x, T, x, Bm, 'grid', LIGHT.grid));
      g.push(text(x, Bm + 20, `${Number((l * 100).toFixed(1))}%`, { cls: 'muted', size: 12, anchor: 'middle' }));
    }
    g.push(text((p.L + p.R) / 2, Bm + 42, 'Packet loss rate', { cls: 'ink2', size: 12, anchor: 'middle' }));
    scen.forEach((s, i) => {
      for (const repair of ['off', 'on']) {
        const pts = rows.filter(r => r.scenario === s && r.repair === repair).sort((a, b) => Number(a.loss) - Number(b.loss));
        g.push(path(pts.map(r => [X(Number(r.loss)), Y(p.val(r))]), i, repair === 'off' ? '6 4' : undefined));
        for (const r of pts) {
          const tip = `${pretty(s)} · loss ${(Number(r.loss) * 100).toFixed(0)}% · repair ${repair}\nmean error ${(Number(r.err_mean_present_m) * 100).toFixed(2)} cm (present) · p95 ${(Number(r.err_p95_present_m) * 100).toFixed(2)} cm\navailability ${(Number(r.availability) * 100).toFixed(2)}% · ${Number(r.bytes_per_s).toFixed(1)} B/s · phantom up to ${Number(r.phantom_max_s).toFixed(1)} s`;
          g.push(marker(X(Number(r.loss)), Y(p.val(r)), i, tip, 4, repair === 'off'));
        }
      }
    });
  });
  const foot = text(32, H - 18, meta.note ?? 'Error is measured at every logged frame against the ground-truth track; a missing entity counts towards availability, not error. Table: summary.md.', { cls: 'muted', size: 11 });
  return svgOpen(W, H, 'Twin under packet loss', 'Two panels against packet loss rate: mean twin error of present entities, and availability, per scenario, with state repair on (solid) and off (dashed).')
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
