// Link and baseline arithmetic shared by the credits row, the V1 panel and the waterfall. Every
// field added for the hackathon is optional here, so the viewer still runs against an older server.
import type { AirtimeModel, Snapshot } from './types';

export const fmtBps = (b: number) => b >= 1e6 ? `${(b / 1e6).toFixed(2)} Mbps` : b >= 1e3 ? `${(b / 1e3).toFixed(1)} kbps` : `${Math.round(b)} bps`;
/** Rounded to what a human needs (STYLE.md, Restraint): kbps with one decimal, error in whole cm. */
export const fmtKbps = (b: number) => `${(b / 1000).toFixed(1)} kbps`;
export const fmtTimes = (x: number) => `${Math.round(x).toLocaleString('en-US')}×`;
const one = (x: number) => String(Math.round(x * 10) / 10);
/** A link's nominal rate the way the profile table states it: 2 kbit/s, 9.6 kbit/s, 600 bit/s. */
export const fmtRate = (bps: number) => bps >= 1e6 ? `${one(bps / 1e6)} Mbit/s` : bps >= 1e3 ? `${one(bps / 1e3)} kbit/s` : `${Math.round(bps)} bit/s`;
export const fmtBytes = (b: number) => b >= 1e6 ? `${one(b / 1e6)} MB` : b >= 1e4 ? `${Math.round(b / 1e3)} KB` : b >= 1e3 ? `${one(b / 1e3)} KB` : `${Math.round(b)} B`;
/** Seconds as m:ss (h:mm:ss past an hour), for countdowns. */
export function fmtClock(s: number): string {
  const t = Math.max(0, Math.ceil(s)), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), ss = String(t % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}
/** Seconds for an interval label: 0.9 s, 2.5 s, 12 s, 2 min. */
export const fmtSeconds = (s: number) => s < 10 ? `${one(s)} s` : s < 120 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`;
/** An error radius: whole cm below a metre, then metres. */
export const fmtRadius = (m: number) => m < 1 ? `${Math.round(m * 100)} cm` : m < 10 ? `${one(m)} m` : `${Math.round(m)} m`;

// ---- H.264 reference (Baseline A) --------------------------------------------------------------
/** Assumed frame rate for the per-frame size (the table carries bitrates only). */
export const VIDEO_FPS = 30;
export interface H264 { id: string; label: string; bps: number; measured: boolean; source: string }
const CONFIGURED: Record<string, number> = { '360': 250_000, '480': 500_000, '720': 1_500_000, '1080': 3_000_000 };
/** H.264 at a resolution: `snap.baselineA` (measured or configured), else the legacy `snap.baselines`, else the built-in table. */
export function h264(snap: Snapshot | null, res: string): H264 {
  // The server's ids are tools/eval's (`h264_720p`); accept the bare forms too.
  const e = snap?.baselineA?.find(b => b.id === `h264_${res}p` || b.id === `${res}p` || b.id === res);
  if (e) return e;
  const legacy = res === '720' ? snap?.baselines?.h264_720p_bps : res === '480' ? snap?.baselines?.h264_480p_bps : undefined;
  return { id: `${res}p`, label: `H.264 ${res}p`, bps: legacy ?? CONFIGURED[res] ?? CONFIGURED['720'], measured: false, source: 'configured' };
}

// ---- the link -------------------------------------------------------------------------------
/** Blackout: everything is lost (a timed override, the blackout profile, or a contested gap). */
export const linkDown = (snap: Snapshot) => snap.shaper.enabled && snap.shaper.loss >= 1;
/** LoRa coding rate as the x of 4/x, whether the model stores the denominator (5) or Semtech's index (1). */
const crDen = (cr: number) => cr <= 4 ? cr + 4 : cr;
/** Raw bit rate of an airtime model (LoRa: SF x BW / 2^SF x 4/CR), 0 when it has none. */
export function modelRate(m: AirtimeModel | undefined): number {
  if (!m || m.kind === 'none') return 0;
  if (m.kind === 'serial') return m.rateBps * 8 / m.bitsPerByte;
  return m.sf * m.bwHz / 2 ** m.sf * 4 / crDen(m.cr);
}
/**
 * What the link carries per second when something limits it, else null (unshaped: video flows).
 * In-process shaper cap first; with the Pi box shaping ('external') the airtime model's raw rate,
 * since the local shaper is off.
 */
export function linkRate(snap: Snapshot): number | null {
  if (snap.shaper.enabled && snap.shaper.bps > 0) return snap.shaper.bps;
  if (snap.link?.profile === 'external') return modelRate(snap.link.model) || null;
  return null;
}
/** The honest competitor (S23): one 150 B chip plus the 28 B UDP/IP header, at the link rate or the edge budget. */
export const THUMB_BYTES = 150, UDP_IP = 28;
export function thumbnailEvery(snap: Snapshot): number | null {
  const rate = Math.min(linkRate(snap) ?? Infinity, snap.budgetBps > 0 ? snap.budgetBps : Infinity);
  return Number.isFinite(rate) ? (THUMB_BYTES + UDP_IP) * 8 / rate : null;
}
/** Airtime model in a few words: `LoRa SF11 · 250 kHz · 4/5`, `serial 9.6 kbit/s`. */
export function modelText(m: AirtimeModel | undefined): string {
  if (!m || m.kind === 'none') return '';
  if (m.kind === 'serial') return `serial ${fmtRate(m.rateBps)}`;
  return `LoRa SF${m.sf} · ${m.bwHz / 1000} kHz · 4/${crDen(m.cr)}`;
}
