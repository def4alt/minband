// Link profiles and time on air (S2). One table, the same as the Pi link box
// (docs/HACKATHON_PLAN.md 3.3), so the in-process shaper and the box emulate the same links.
// Applying a profile sets the shaper (rate, delay, loss, queue), the edge budget and the airtime
// model; 'external' turns the shaper off (the box shapes) and takes only the budget and model of
// the profile named by `as`; 'contested' alternates lora with random blackouts on a timer.
//
// What counts on air: the datagram's payload (MinBand's message bytes) plus the model's
// `overheadBytes` of radio framing, NOT the 28 B UDP/IPv4 header. A LoRa or serial radio carries
// the payload in its own frame; the IP header only exists on the Wi-Fi/Ethernet hops of the emulation.
// The shaper, the bytes graph and PacketEvent.bytes keep counting +28 B because that is what the
// emulated link carries. Time on air is counted from delivered datagrams, so it is a lower bound
// under loss: a datagram lost on air still used the channel.
import type { AirtimeModel, LinkProfile, LinkView, ShaperConfig } from './types.js';
import { DEFAULT_SHAPER, sanitizeShaper, type Shaper } from './shaper.js';

export type LoraModel = Extract<AirtimeModel, { kind: 'lora' }>;
export const NO_AIRTIME: AirtimeModel = { kind: 'none' };
/** Meshtastic LongFast: SF11, 250 kHz, CR 4/5 (`cr` is the denominator, 5..8 = 4/5..4/8, as in
 * Meshtastic and RadioLib), 16-symbol preamble, explicit header, CRC on, LDRO off. Raw LoRa PHY:
 * a Meshtastic transport would add its own 16 B packet header (`overheadBytes: 16`). */
export const LORA_LONGFAST: LoraModel = { kind: 'lora', sf: 11, bwHz: 250_000, cr: 5, preamble: 16, crc: true, explicitHeader: true, lowDataRateOptimize: false, overheadBytes: 0 };
/** Serial-class radio fed by a UART: 8N1 = 10 bits per byte at the link rate, plus two SLIP-style
 * frame delimiters per datagram (escapes ignored). A synchronous HF modem would be 8 bits/byte. */
export const serialModel = (rateBps: number): AirtimeModel => ({ kind: 'serial', rateBps, bitsPerByte: 10, overheadBytes: 2 });

/** Semtech LoRa time on air (SX127x datasheet / AN1200.13), SF7..12. */
export function loraAirtimeMs(m: LoraModel, payloadBytes: number): number {
  const tSym = 2 ** m.sf / m.bwHz * 1000;
  const de = m.lowDataRateOptimize ? 1 : 0, ih = m.explicitHeader ? 0 : 1, crc = m.crc ? 1 : 0;
  const pl = payloadBytes + m.overheadBytes;
  const blocks = Math.ceil((8 * pl - 4 * m.sf + 28 + 16 * crc - 20 * ih) / (4 * (m.sf - 2 * de)));
  return (m.preamble + 4.25 + 8 + Math.max(blocks * m.cr, 0)) * tSym;
}

/** Channel time (ms) one datagram of `payloadBytes` (without UDP/IP) takes under `m`; 0 for 'none'. */
export function airtimeMs(m: AirtimeModel, payloadBytes: number): number {
  switch (m.kind) {
    case 'lora': return loraAirtimeMs(m, payloadBytes);
    case 'serial': return (payloadBytes + m.overheadBytes) * m.bitsPerByte / m.rateBps * 1000;
    default: return 0;
  }
}

const prof = (name: string, bps: number, delayMs: number, loss: number, queue: number, budgetBps: number, airtime: AirtimeModel, label: string): LinkProfile =>
  ({ name, bps, delayMs, loss, queue, budgetBps, airtime, label });

/** docs/HACKATHON_PLAN.md 3.3. bps/queue 0 = none. */
export const PROFILES: readonly LinkProfile[] = [
  prof('clean', 0, 0, 0, 0, 0, NO_AIRTIME, 'Wi-Fi reference'),
  prof('degraded', 64_000, 20, 0.02, 20, 0, NO_AIRTIME, 'Busy mesh'),
  prof('hf', 9_600, 500, 0.01, 8, 8_000, serialModel(9_600), 'NATO HF ceiling'),
  prof('lora', 2_000, 300, 0.10, 4, 1_500, LORA_LONGFAST, 'Meshtastic-class LoRa'),
  prof('telemetry', 600, 50, 0.05, 4, 450, serialModel(600), 'ELRS-class control-link telemetry'),
  prof('contested', 2_000, 300, 0.10, 4, 1_500, LORA_LONGFAST, 'Intermittent jamming (lora + 1-5 s blackouts)'),
  prof('blackout', 0, 0, 1, 0, 0, NO_AIRTIME, 'Link cut'),
];
export const findProfile = (name: string): LinkProfile | undefined => PROFILES.find(p => p.name === name);

/** contested: lora for `onMs`, then a blackout (100 % loss) for `blackoutMs`, each uniform in [lo, hi]. */
export const CONTESTED = { onMs: [3_000, 8_000], blackoutMs: [1_000, 5_000] } as const;

/** Shaper config for a profile ('clean' = shaper off, everything reset). */
export function shaperFor(p: LinkProfile): ShaperConfig {
  if (p.name === 'clean') return { ...DEFAULT_SHAPER };
  return { ...DEFAULT_SHAPER, enabled: true, bps: p.bps, delayMs: p.delayMs, loss: p.loss, queue: p.queue };
}

/** Error message for an invalid (profile, as) pair, else null. */
export function linkError(name: unknown, as?: unknown): string | null {
  const names = PROFILES.map(p => p.name);
  if (typeof name !== 'string' || (name !== 'external' && !findProfile(name))) return `profile must be one of ${[...names, 'external'].join(', ')}`;
  if (as === undefined) return null;
  if (name !== 'external') return 'as only applies to profile=external';
  if (typeof as !== 'string' || !findProfile(as)) return `as must be one of ${names.join(', ')}`;
  return null;
}

export interface LinkDeps {
  shaper: Shaper;
  setBudget: (bps: number) => void;
  note?: (line: string) => void;
  now?: () => number;
  /** contested blackout/on lengths */
  rng?: () => number;
  schedule?: (fn: () => void, ms: number) => unknown;
  cancel?: (handle: unknown) => void;
}

/** The active link: profile name, airtime model and the contested loop. Starts as 'clean'. */
export class Link {
  profile = 'clean';
  as: string | null = null;
  model: AirtimeModel = NO_AIRTIME;
  private loop: { blackout: boolean; until: number; handle: unknown } | null = null;
  private readonly d: Required<LinkDeps>;

  constructor(deps: LinkDeps) {
    this.d = {
      note: () => {}, now: Date.now, rng: Math.random,
      schedule: (fn, ms) => setTimeout(fn, ms), cancel: h => clearTimeout(h as ReturnType<typeof setTimeout>),
      ...deps,
    };
  }

  /** Apply a named profile, or 'external' with the profile the box emulates. Returns an error and
   * changes nothing when the names are invalid. */
  apply(name: unknown, as?: unknown): string | null {
    const err = linkError(name, as);
    if (err) { this.d.note(`link: ${err}`); return err; }
    this.stop();
    const ext = name === 'external';
    const p = findProfile(ext ? (as as string | undefined) ?? 'clean' : name as string)!;
    this.d.shaper.set(ext ? { ...DEFAULT_SHAPER } : shaperFor(p));
    this.d.setBudget(p.budgetBps);
    this.model = p.airtime; this.profile = ext ? 'external' : p.name; this.as = ext ? p.name : null;
    this.d.note(`link: ${ext ? `external as ${p.name}` : p.name} (budget ${p.budgetBps} bit/s)`);
    if (p.name === 'contested' && !ext) this.phase(false);
    return null;
  }

  /** A hand-made shaper change (viewer sliders, /api/shaper). A timed override (blackout preset)
   * keeps the profile, since the shaper restores the profile's config afterwards (a contested
   * phase switch ends it early); any other change makes the link 'custom' and keeps the airtime
   * model (the radio is the same, only its impairment changed). No fields: ends a timed override. */
  manual(c: Partial<ShaperConfig>, revertAfterMs?: number): ShaperConfig {
    const { ok } = sanitizeShaper(c as Record<string, unknown>);
    const r = Number(revertAfterMs);
    if (revertAfterMs !== undefined && Number.isFinite(r) && r > 0) return this.d.shaper.setFor(ok, Math.min(r, 3_600_000));
    if (Object.keys(ok).length && this.profile !== 'custom') {
      if (this.loop?.blackout) this.d.shaper.set(shaperFor(findProfile('lora')!)); // do not keep the jammer's 100 % loss
      this.stop(); this.profile = 'custom'; this.as = null;
      this.d.note('link: custom (shaper set by hand)');
    }
    return this.d.shaper.set(ok);
  }

  /** Stop the contested loop (another profile, or shutdown). */
  stop() {
    if (this.loop) { this.d.cancel(this.loop.handle); this.loop = null; }
  }

  private phase(blackout: boolean) {
    const lora = shaperFor(findProfile('lora')!);
    this.d.shaper.set(blackout ? { ...lora, loss: 1 } : lora);
    const [lo, hi] = blackout ? CONTESTED.blackoutMs : CONTESTED.onMs;
    const ms = Math.round(lo + this.d.rng() * (hi - lo));
    const handle = this.d.schedule(() => { if (this.loop?.handle === handle) this.phase(!blackout); }, ms);
    this.loop = { blackout, until: this.d.now() + ms, handle };
    if (blackout) this.d.note(`link: contested blackout ${(ms / 1000).toFixed(1)} s`);
  }

  view(up: { airtimeShare: number; msgsPerSec: number }, down?: { airtimeShare: number; msgsPerSec: number }): LinkView {
    const v: LinkView = { profile: this.profile, model: this.model, airtimeShare: up.airtimeShare, msgsPerSec: up.msgsPerSec, profiles: [...PROFILES] };
    if (this.as !== null) v.as = this.as;
    if (down) { v.downAirtimeShare = down.airtimeShare; v.downMsgsPerSec = down.msgsPerSec; }
    if (this.loop) v.contested = { blackout: this.loop.blackout, switchInMs: Math.max(0, this.loop.until - this.d.now()) };
    return v;
  }
}
