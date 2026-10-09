// Minimal CSV for result tables. Cells containing a comma, quote or newline are quoted.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export type Cell = string | number | boolean;
export type Row = Record<string, Cell>;

function cell(v: Cell): string {
  if (typeof v === 'boolean') return String(v);
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return '';
    return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(6)));
  }
  return /[",\n]/.test(v) ? `"${v.replaceAll('"', '""')}"` : v;
}

export function writeCsv(path: string, columns: string[], rows: Row[]): void {
  mkdirSync(dirname(path), { recursive: true });
  const lines = [columns.join(',')];
  for (const r of rows) lines.push(columns.map(c => cell(r[c] ?? '')).join(','));
  writeFileSync(path, lines.join('\n') + '\n');
}

function splitLine(line: string): string[] {
  const out: string[] = [];
  let cur = '', q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (q) {
      if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c;
    } else if (c === '"') q = true;
    else if (c === ',') { out.push(cur); cur = ''; } else cur += c;
  }
  out.push(cur);
  return out;
}

/** Rows as records; numeric-looking cells become numbers. */
export function readCsv(path: string): Record<string, string | number>[] {
  const lines = readFileSync(path, 'utf8').split(/\r?\n/).filter(l => l.trim());
  if (!lines.length) return [];
  const cols = splitLine(lines[0]);
  return lines.slice(1).map(l => {
    const f = splitLine(l);
    const r: Record<string, string | number> = {};
    cols.forEach((c, i) => { const v = f[i] ?? ''; r[c] = v !== '' && Number.isFinite(Number(v)) ? Number(v) : v; });
    return r;
  });
}
