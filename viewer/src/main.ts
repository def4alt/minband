import { LINE_STYLES, TwinScene } from './scene';
import { Halftone } from './halftone';
import { VideoOnLink } from './videolink';
import { Clicker, Waterfall } from './waterfall';
import { fmtBps, fmtKbps, fmtRate, fmtSeconds, fmtTimes, h264, linkDown, linkRate, modelText } from './link';
import type { ControlMessage, LinkProfile, PacketEvent, ShaperConfig, Snapshot } from './types';

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const INK = css('--ink'), INK2 = css('--ink-2'), INK3 = css('--ink-3'), INK4 = css('--ink-4');
const MONO = css('--mono');
const LINK_IN = css('--series-1'), SENT = css('--series-4'); // graph series: colour, not dash

const scene = new TwinScene($('scene'));
// Dev server only: lets dev/smoke.ts find an entity on screen to hover.
if (import.meta.env.DEV) Object.assign(window, { __minband: { scene } });
// Wide area (real drone footage): the FRAME control and the grid spacing in the key; F re-frames too.
scene.onWide = (wide, cellM) => {
  $('frame').hidden = !wide; $('keyGrid').hidden = !wide;
  if (wide) setText('keyGridV', `${cellM} m grid`);
};
$('frame').onclick = () => scene.frame();
addEventListener('keydown', e => {
  if (e.key.toLowerCase() !== 'f' || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
  if ((e.target as HTMLElement | null)?.closest?.('input, textarea, select, [contenteditable]')) return;
  scene.frame();
});
const WS_URL = (import.meta.env.VITE_WS_URL as string | undefined) ?? `ws://${location.hostname}:8080`;
// The HTTP API is served by the same server as the WebSocket.
const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? WS_URL.replace(/^ws/, 'http');
let ws: WebSocket;
const send = (m: ControlMessage) => ws?.readyState === 1 && ws.send(JSON.stringify(m));

const fmtCm = (m: number) => `${Math.round(m * 100)} cm`;
const fmtPct = (x: number) => `${Math.round(x * 100)} %`;
const esc = (s: string) => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]!));
/** Inline markup: dim separators between credit values. */
const SEP = (c = '·') => ` <span class="sep">${c}</span> `;
/** CSS blink phase-locked to the scene's 1 Hz clock, so elements rebuilt every snapshot keep blinking (lost devices only). */
const phase = () => `style="animation-delay:-${Math.round(performance.now() % 1000)}ms"`;
// Slider stops: log scale from 500 bps to 2 Mbps, 0 = off.
const STEPS = 200, LO = 500, HI = 2e6;
const capValue = (v: number) => v === 0 ? 0 : Math.round(LO * Math.pow(HI / LO, (v - 1) / (STEPS - 1)));
const capSlider = (bps: number) => bps <= 0 ? 0 : Math.max(1, Math.min(STEPS, Math.round(1 + (STEPS - 1) * Math.log(bps / LO) / Math.log(HI / LO))));

/** Set the visible text only when it changed (avoids restarting CSS animations and layout churn). */
function setHTML(id: string, html: string) { const el = $(id); if (el.innerHTML !== html) el.innerHTML = html; }
function setText(id: string, text: string) { const el = $(id); if (el.textContent !== text) el.textContent = text; }
const buttons = (sel: string) => Array.from(document.querySelectorAll<HTMLButtonElement>(sel));
const params = new URLSearchParams(location.search);

/**
 * Numbers never jump (STYLE.md): airtime over the server's 2 s window swings on slow links, where
 * one keyframe can hold the channel for seconds, so readouts show it smoothed over ~3 s.
 */
const smoothed = new Map<string, { v: number; t: number }>();
function smooth(key: string, v: number, t: number, tauMs = 3000): number {
  const s = smoothed.get(key);
  if (!s || t < s.t || t - s.t > 10_000) { smoothed.set(key, { v, t }); return v; }
  s.v += (v - s.v) * (1 - Math.exp(-(t - s.t) / tauMs)); s.t = t;
  return s.v;
}

// ---- link profiles (S2) -----------------------------------------------------------------------
// The server's profile table (`snap.link.profiles`, HACKATHON_PLAN 3.3) drives the selector. These
// shaper-only scenarios are the fallback for a server without `snap.link`.
const SCENARIOS: Record<string, Partial<ShaperConfig>> = {
  clean: { enabled: false, bps: 0, delayMs: 0, loss: 0 },
  contested: { enabled: true, bps: 50_000, delayMs: 300, loss: 0.05 },
  jammed: { enabled: true, bps: 2_000, delayMs: 800, loss: 0.3 },
};
const BLACKOUT_MS = 10_000;
function scenarioOf(s: ShaperConfig, revertMs: number | null): string {
  if (revertMs !== null && s.enabled && s.loss >= 1) return 'blackout';
  if (!s.enabled || (!s.bps && !s.delayMs && !s.loss)) return 'clean';
  for (const [name, p] of Object.entries(SCENARIOS)) if (p.enabled && p.bps === s.bps && p.delayMs === s.delayMs && Math.abs((p.loss ?? 0) - s.loss) < 1e-9) return name;
  return 'custom';
}
const timedBlackout = (snap: Snapshot) => snap.shaperRevertMs != null && snap.shaper.enabled && snap.shaper.loss >= 1;
/** The shaper settings, for a link set by hand. */
const shaperDesc = (s: ShaperConfig) => [s.enabled && s.bps ? fmtRate(s.bps) : 'uncapped', `${Math.round(s.delayMs)} ms`, `${Math.round(s.loss * 100)}%`].join(SEP());
/** Credits row, Link: `lora 2 kbit/s · 68 % airtime`, `blackout · 7 s left`, or the settings when set by hand. */
function linkCredit(snap: Snapshot): string {
  const s = snap.shaper, l = snap.link;
  if (timedBlackout(snap)) return `blackout${SEP()}${Math.ceil(snap.shaperRevertMs! / 1000)} s left`;
  if (!l) { const name = scenarioOf(s, snap.shaperRevertMs ?? null); return name === 'custom' ? shaperDesc(s) : name; }
  const rate = linkRate(snap);
  const head = l.profile === 'custom' ? shaperDesc(s) : `${esc(l.profile)}${rate ? ` ${fmtRate(rate)}` : ''}`;
  const down = linkDown(snap) && l.profile !== 'blackout' ? `${SEP()}link down` : '';
  const air = l.model && l.model.kind !== 'none' ? `${SEP()}${fmtPct(smooth('link', l.airtimeShare, snap.t))} airtime` : '';
  return `${head}${down}${air}`;
}

const profileParams = (p: LinkProfile) => p.loss >= 1 ? '100% loss' : !p.bps && !p.delayMs && !p.loss ? 'no impairment'
  : [p.bps ? fmtRate(p.bps) : 'uncapped', `${Math.round(p.delayMs)} ms`, `${Math.round(p.loss * 100)}%`].join(' · ');
let profileSig = '';
function renderProfiles(profiles: LinkProfile[]) {
  // A held 'blackout' profile is left out: the timed action below is the stage-safe cut.
  const shown = profiles.filter(p => p.name !== 'blackout');
  const sig = shown.map(p => `${p.name}:${p.bps}:${p.delayMs}:${p.loss}:${p.label}`).join(',');
  if (sig === profileSig) return;
  profileSig = sig;
  $('scenario').innerHTML = shown.map(p => `<button data-profile="${esc(p.name)}" title="${esc(p.label)}">${esc(p.name)}<span class="p">${profileParams(p)}</span></button>`).join('')
    + `<button data-blackout="1" title="100% loss for 10 s, then the link comes back">Blackout 10 s<span class="p">100% loss</span></button>`;
}
/** Header of the profile section: what the active profile stands for. */
function profileNote(snap: Snapshot): string {
  const l = snap.link!;
  if (timedBlackout(snap)) return `blackout · ${Math.ceil(snap.shaperRevertMs! / 1000)} s left`;
  if (l.profile === 'external') return `external${l.model.kind !== 'none' ? ` · ${modelText(l.model)}` : ''}`;
  return l.profiles.find(p => p.name === l.profile)?.label ?? l.profile;
}

// ---- bytes graph: oscilloscope ---------------------------------------------------------------
const HIST = 300; // 10 s at 30 Hz
const history: { total: number; offered: number }[] = [];
function drawGraph(snap: Snapshot) {
  const c = $<HTMLCanvasElement>('graph'), ctx = c.getContext('2d')!;
  const dpr = Math.min(devicePixelRatio || 1, 2), W = c.clientWidth, H = c.clientHeight;
  if (!W || !H) return;
  if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (!history.length) return;
  const total = history[history.length - 1].total;
  const cap = snap.shaper.enabled && snap.shaper.bps ? snap.shaper.bps : 0;
  const max = 1.6 * Math.max(1000, ...history.map(h => Math.max(h.total, snap.shaper.enabled ? h.offered : 0)), cap);
  // Log scale from 100 bps (the floor) to the auto max; the top band carries the off-scale label.
  const FLOOR = 100, top = 18, bottom = H - 0.5;
  const y = (v: number) => bottom - Math.max(0, Math.log10(Math.max(v, FLOOR) / FLOOR) / Math.log10(max / FLOOR)) * (bottom - top);
  const px = (v: number) => Math.round(v) + 0.5; // crisp hairlines
  ctx.clearRect(0, 0, W, H);
  ctx.lineWidth = 1;
  ctx.font = `400 8.5px ${MONO}`; ctx.textBaseline = 'middle';

  // hairline grid: 10 time divisions, one line per decade
  ctx.strokeStyle = INK4; ctx.beginPath();
  for (let i = 0; i <= 10; i++) { const x = px((i / 10) * (W - 1)); ctx.moveTo(x, top); ctx.lineTo(x, bottom); }
  ctx.moveTo(0, px(bottom)); ctx.lineTo(W, px(bottom)); ctx.moveTo(0, px(top)); ctx.lineTo(W, px(top));
  const decades: number[] = []; for (let d = 1e3; d < max; d *= 10) decades.push(d);
  for (const d of decades) { const yy = px(y(d)); ctx.moveTo(0, yy); ctx.lineTo(W, yy); }
  ctx.stroke();
  ctx.fillStyle = INK3; ctx.textAlign = 'right';
  for (const d of decades) ctx.fillText(d >= 1e6 ? `${d / 1e6}M` : d >= 1e3 ? `${d / 1e3}k` : `${d}`, W - 3, y(d) - 5);

  // cap: dashed reference
  if (cap) {
    const yy = px(y(cap));
    ctx.strokeStyle = INK2; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(W, yy); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = INK2; ctx.textAlign = 'left'; ctx.fillText(`CAP ${fmtBps(cap)}`, 3, yy - 5);
  }
  // H.264 baseline (Baseline A, "configured" until measured): a dashed reference when it is on the
  // scale, else a label at the top edge saying how far over
  const v = h264(snap, '720'), name = `${v.label.toUpperCase()}${v.measured ? '' : ' · CONFIGURED'}`;
  if (v.bps <= max) {
    const yy = px(y(v.bps));
    ctx.strokeStyle = INK2; ctx.setLineDash([1.5, 3]); ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(W, yy); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = INK2; ctx.textAlign = 'left'; ctx.fillText(name, 3, yy - 5);
  } else {
    ctx.fillStyle = INK2; ctx.textAlign = 'left';
    ctx.fillText(`▲ ${name} ${fmtBps(v.bps)} · ${fmtTimes(v.bps / Math.max(total, 1))} above`, 0, 6);
  }

  const trace = (pick: (h: { total: number; offered: number }) => number, color: string) => {
    ctx.strokeStyle = color; ctx.beginPath();
    history.forEach((h, i) => { const xx = (i / (HIST - 1)) * W, yy = y(pick(h)); i ? ctx.lineTo(xx, yy) : ctx.moveTo(xx, yy); });
    ctx.stroke();
  };
  if (snap.shaper.enabled) trace(h => h.offered, SENT); // what the edge sent, before the link
  trace(h => h.total, LINK_IN);
  // beam head
  const last = history[history.length - 1], hx = ((history.length - 1) / (HIST - 1)) * W;
  ctx.fillStyle = LINK_IN; ctx.fillRect(Math.min(W - 2, hx - 1), y(last.total) - 1, 2, 2);
}

// ---- controls synced from the server ---------------------------------------------------------
// Sliders reflect the server's shaper (it may be driven from /api/shaper or another viewer),
// except while the operator is touching one.
const touched = new Map<string, number>();
const touch = (id: string) => touched.set(id, performance.now());
const idle = (id: string) => performance.now() - (touched.get(id) ?? -1e9) > 1500;
/** Paint the 2 px filled portion of a slider. */
const paint = (el: HTMLInputElement) => el.style.setProperty('--f', String((+el.value - +el.min) / Math.max(1, +el.max - +el.min)));
const setSlider = (id: string, v: number) => { const el = $<HTMLInputElement>(id); if (idle(id)) { el.value = String(v); paint(el); } };
function setToggle(id: string, on: boolean) {
  const b = $(id); b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on));
  const v = b.querySelector('.v'); if (v && v.textContent !== (on ? 'on' : 'off')) v.textContent = on ? 'on' : 'off';
}
const isOn = (id: string) => $(id).classList.contains('on');

/** Highlight the active profile (or fallback scenario) unless the operator just clicked one. */
function syncProfiles(snap: Snapshot) {
  if (!idle('scenario')) return;
  if (!snap.link) {
    const name = scenarioOf(snap.shaper, snap.shaperRevertMs ?? null);
    for (const b of buttons('#scenario button')) b.classList.toggle('on', b.dataset.scenario === name);
    setText('scenarioName', name);
    return;
  }
  renderProfiles(snap.link.profiles);
  const timed = timedBlackout(snap), active = timed ? 'blackout' : snap.link.profile;
  for (const b of buttons('#scenario button')) {
    const on = b.dataset.blackout ? active === 'blackout' : b.dataset.profile === active;
    b.classList.toggle('on', on);
    if (b.dataset.blackout) { const p = b.querySelector('.p')!; const t = timed ? `${Math.ceil(snap.shaperRevertMs! / 1000)} s left` : '100% loss'; if (p.textContent !== t) p.textContent = t; }
  }
  setText('scenarioName', profileNote(snap));
}

function syncControls(snap: Snapshot) {
  const s = snap.shaper;
  setSlider('capSlider', capSlider(s.bps)); setText('bpsv', s.bps ? fmtBps(s.bps) : 'off');
  setSlider('delaySlider', s.delayMs); setText('delayv', `${Math.round(s.delayMs)} ms`);
  setSlider('lossSlider', Math.round(s.loss * 100)); setText('lossv', `${Math.round(s.loss * 100)}%`);
  const budget = snap.budgetBps ?? 0;
  setSlider('budgetSlider', capSlider(budget)); setText('budgetv', budget ? fmtBps(budget) : 'unlimited');
  setToggle('impair', s.enabled);
  setToggle('fusion', snap.fusion);
  syncProfiles(snap);
}

// ---- render ----------------------------------------------------------------------------------
let lastTotal = 0;
let lastSnap: Snapshot | null = null;
const swatch = (style: number) => `<svg width="24" height="5" viewBox="0 0 24 5" aria-hidden="true"><line x1="0" y1="2.5" x2="24" y2="2.5" stroke="${INK}" stroke-width="1"${LINE_STYLES[style].svg ? ` stroke-dasharray="${LINE_STYLES[style].svg}"` : ''}/></svg>`;
const detailsOpen = () => !$('details').hidden;
const waterfall = new Waterfall($<HTMLCanvasElement>('waterfall'));
const clicker = new Clicker();

function render(snap: Snapshot) {
  lastSnap = snap;
  scene.update(snap);
  setStatus(null);
  const total = snap.devices.reduce((a, d) => a + d.bps, 0); lastTotal = total;
  const hasAir = !!snap.link?.model && snap.link.model.kind !== 'none';

  // default view: one primary readout, the twin error, the credits row
  setHTML('bps', `${(total / 1000).toFixed(1)}<small>kbps</small>`); setText('sheetBps', fmtKbps(total));
  const n = snap.devices.length, lost = snap.devices.filter(d => d.silent).length;
  const coasting = snap.devices.filter(d => d.coasting && !d.silent).length;
  setHTML('cEdge', n ? `${n} device${n === 1 ? '' : 's'}${coasting ? `${SEP()}${coasting === n ? 'coasting' : `${coasting} coasting`}` : ''}${lost ? `${SEP()}<span class="lost" ${phase()}>${lost} lost</span>` : ''}` : 'no device');
  setHTML('cLink', linkCredit(snap));
  // Grid reference of the marker origin (S3) when the server has a geodetic anchor.
  setHTML('cTwin', `${snap.global.length} entit${snap.global.length === 1 ? 'y' : 'ies'}${snap.geo ? `${SEP()}${esc(snap.geo.mgrs)}` : ''}`);
  syncControls(snap);
  history.push({ total, offered: snap.devices.reduce((a, d) => a + (d.offeredBps ?? d.bps), 0) }); if (history.length > HIST) history.shift();

  waterfall.push(snap);
  clicker.play(snap.packets, snap.t);
  if (stageOn || detailsOpen()) waterfall.draw(h264(snap, res));
  if ($('camera').hidden === false) updateRawOverlay(snap);
  if (!detailsOpen()) return;

  // details
  const v = h264(snap, '720'), b = snap.baselines, notes = [
    `${v.label}${v.measured ? '' : ' (configured)'} would be ${fmtBps(v.bps)}, ${fmtTimes(v.bps / Math.max(total, 1))} more`,
    `naive 30 Hz metadata ${fmtBps(b.naiveMetadataBps)}`,
  ];
  if (v.measured) notes.push(`measured: ${esc(v.source)}`);
  if (hasAir) notes.push(`airtime ${fmtPct(smooth('link', snap.link!.airtimeShare, snap.t))} of ${modelText(snap.link!.model)}${SEP()}${snap.link!.msgsPerSec.toFixed(1)} msg/s`);
  setHTML('baselines', notes.join('<br>'));
  $('devices').innerHTML = snap.devices.map(d => {
    const dk = d.key ?? String(d.deviceId), style = scene.styleOf(dk);
    const name = d.provisional ? `Dev ?${SEP()}<span class="note">${esc(d.addr)}</span>` : `Dev ${d.deviceId}`;
    const tags = [d.silent ? `<span class="tag lost" ${phase()}>lost</span>` : d.coasting ? '<span class="tag">coasting</span>' : '', d.addrChanges ? `<span class="tag">moved ×${d.addrChanges}</span>` : ''].join(' ');
    const sent = snap.shaper.enabled && d.offeredBps > d.bps * 1.05 ? `<div class="kv"><span class="note">sent before the link</span><span class="note">${fmtBps(d.offeredBps)}</span></div>` : '';
    // Share of channel time (S2) and the heartbeat the budget gives this device (S19).
    const air = hasAir && d.airtimeShare !== undefined ? `${SEP()}${fmtPct(smooth(`dev ${dk}`, d.airtimeShare, snap.t))} airtime` : '';
    const beat = d.cadence ? `${SEP()}keyframe every ${fmtSeconds(d.cadence.keyframeMs / 1000)}` : '';
    return `<div class="dev"><div class="kv"><span class="name lbl">${swatch(style)}<span>${name}</span>${tags}</span><span class="val">${fmtBps(d.bps)}</span></div>`
      + `<div class="kv"><span class="note">${d.entities.length} ent${SEP()}${d.msgsPerSec.toFixed(1)} msg/s${air}</span><span class="note dim">${LINE_STYLES[style].name}</span></div>${sent}`
      + `<div class="note dim">kf ${d.stats.keyframes ?? 0}${SEP()}Δ ${d.stats.deltas ?? 0}${SEP()}gaps ${d.stats.gapsDetected ?? 0}${SEP()}nacks ${d.stats.nacksSent ?? 0}${beat}</div></div>`;
  }).join('');
  drawGraph(snap);
}

function setStatus(text: string | null) {
  const el = $('status');
  if (text === null) { el.hidden = true; return; }
  el.hidden = false; setText('status', text);
}

// Render at most once per animation frame, always the newest snapshot: when a frame is slower
// than the 30 Hz feed (software GL, a busy laptop) stale snapshots are dropped instead of queued,
// which otherwise left the view tens of seconds behind the link. Packet events of the skipped
// snapshots are carried into the rendered one, since the waterfall needs every datagram.
let pendingSnap: Snapshot | null = null, frameQueued = false;
const pendingPackets: PacketEvent[] = [];
function queueSnapshot(snap: Snapshot) {
  pendingSnap = snap;
  if (snap.packets?.length) { pendingPackets.push(...snap.packets); if (pendingPackets.length > 4000) pendingPackets.splice(0, pendingPackets.length - 4000); }
  if (frameQueued) return;
  frameQueued = true;
  requestAnimationFrame(() => {
    frameQueued = false;
    const s = pendingSnap; pendingSnap = null;
    if (s) render({ ...s, packets: pendingPackets.splice(0) });
  });
}

function connect() {
  ws = new WebSocket(WS_URL);
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === 'snapshot') queueSnapshot(m.snap);
    else if (m.type === 'log' && m.lines.length) { const l = $('log'); l.textContent = (l.textContent + m.lines.join('\n') + '\n').split('\n').slice(-60).join('\n'); l.scrollTop = l.scrollHeight; }
  };
  ws.onclose = () => { setStatus('no link to server · retrying'); setTimeout(connect, 1000); };
}
connect();

// ---- twin error (HTTP, every 2 s) ------------------------------------------------------------
interface MetricsLite { twinError: { meanM: number | null; p95M: number | null; samples: number }; devices: { twinError: { updatedMs: number } | null }[]; t: number }
async function pollMetrics() {
  try {
    const m = await (await fetch(`${API_URL}/api/metrics`, { cache: 'no-store' })).json() as MetricsLite;
    const te = m.twinError;

    if (te.meanM === null) {
      setText('twinErr', 'no ground truth'); $('twinErr').classList.add('dim');
      setText('twinErrDetail', 'twin error: no ground truth uploaded'); setText('sheetErr', '–');
    } else {
      const updated = Math.max(...m.devices.map(d => d.twinError?.updatedMs ?? 0));
      const age = updated ? Math.max(0, Math.round((m.t - updated) / 1000)) : null;
      setText('twinErr', fmtCm(te.meanM)); $('twinErr').classList.remove('dim');
      setText('twinErrDetail', `twin error: mean ${(te.meanM * 100).toFixed(1)} cm · p95 ${(te.p95M! * 100).toFixed(1)} cm · ${te.samples} GT rows${age !== null ? ` · ${age} s ago` : ''}`);
      setText('sheetErr', fmtCm(te.meanM));
    }
  } catch {
    setText('twinErrDetail', 'twin error: metrics unavailable');
  }
}
pollMetrics(); setInterval(pollMetrics, 2000);

// ---- operator controls -----------------------------------------------------------------------
const shaper = (config: Partial<ShaperConfig>) => send({ type: 'shaper', config });
const onInput = (id: string, fn: (v: number) => void) => {
  const el = $<HTMLInputElement>(id); paint(el);
  el.oninput = () => { touch(id); paint(el); fn(+el.value); };
};
// A manual change makes the server's profile 'custom'.
onInput('capSlider', v => { const b = capValue(v); setText('bpsv', b ? fmtBps(b) : 'off'); shaper({ bps: b }); });
onInput('delaySlider', v => { setText('delayv', `${v} ms`); shaper({ delayMs: v }); });
onInput('lossSlider', v => { setText('lossv', `${v}%`); shaper({ loss: v / 100 }); });
onInput('budgetSlider', v => { const b = capValue(v); setText('budgetv', b ? fmtBps(b) : 'unlimited'); send({ type: 'budget', bps: b }); });
$('impair').onclick = () => { const on = !isOn('impair'); setToggle('impair', on); shaper({ enabled: on }); };
$('fusion').onclick = () => { const on = !isOn('fusion'); setToggle('fusion', on); send({ type: 'fusion', enabled: on }); };
$('ghosts').onclick = () => { scene.showGhosts = !scene.showGhosts; setToggle('ghosts', scene.showGhosts); };
// Profiles are rebuilt from the server's table, so one delegated handler serves them and the fallback scenarios.
$('scenario').addEventListener('click', ev => {
  const b = (ev.target as HTMLElement).closest<HTMLButtonElement>('#scenario button');
  if (!b) return;
  touch('scenario');
  for (const o of buttons('#scenario button')) o.classList.toggle('on', o === b);
  // The server holds the blackout timer and restores the previous link itself (survives a reload).
  if (b.dataset.blackout || b.dataset.scenario === 'blackout') { setText('scenarioName', 'blackout'); send({ type: 'shaper', config: { enabled: true, loss: 1 }, revertAfterMs: BLACKOUT_MS }); }
  else if (b.dataset.profile) { setText('scenarioName', b.dataset.profile); send({ type: 'link', profile: b.dataset.profile }); }
  else if (b.dataset.scenario && SCENARIOS[b.dataset.scenario]) { setText('scenarioName', b.dataset.scenario); shaper(SCENARIOS[b.dataset.scenario]); }
});
$('click').onclick = () => { if (clicker.on) clicker.disable(); else clicker.enable(); setToggle('click', clicker.on); };

// ---- bottom sheet (narrow screens) -----------------------------------------------------------
$('sheetToggle').onclick = () => {
  const open = $('side').classList.toggle('open');
  $('sheetToggle').setAttribute('aria-expanded', String(open));
};

// ---- the one disclosure: DETAILS, collapsed by default, remembered per browser ----------------
const DETAILS_KEY = 'minband.details';
function setDetails(open: boolean) {
  $('details').hidden = !open;
  $('detailsToggle').setAttribute('aria-expanded', String(open));
  try { localStorage.setItem(DETAILS_KEY, open ? '1' : '0'); } catch { /* storage blocked: still works for this page */ }
}
$('detailsToggle').onclick = () => setDetails($('details').hidden);
try { if (localStorage.getItem(DETAILS_KEY) === '1') setDetails(true); } catch { /* default: collapsed */ }
if (params.has('details')) setDetails(params.get('details') !== '0');

// ---- side-by-side: twin lines vs video on the same link (V1) ----------------------------------
let res = '720';
let stream: MediaStream | null = null;
let camWanted = false;
const halftone = new Halftone($<HTMLCanvasElement>('halftone'));
const videoLink = new VideoOnLink(halftone, $<HTMLCanvasElement>('thumb'));

function updateRawOverlay(snap: Snapshot | null) {
  const v = h264(snap, res);
  // "configured" goes away once Baseline A is measured on the phone.
  setHTML('rawRate', `${fmtBps(v.bps)}${v.measured ? '' : ` <span class="dim">configured</span>`}`);
  setText('rawVs', lastTotal > 0 ? `${fmtKbps(lastTotal)} · ${fmtTimes(v.bps / lastTotal)} less` : 'idle');
  if (!snap) return;
  const view = videoLink.update(snap, res);
  setText('vlValue', view.value); setText('vlDetail', view.detail);
  $('frameClock').hidden = view.mode === 'flows';
  setText('frameClockL', view.mode === 'stalled' ? 'Link down' : 'Next frame'); setText('frameClockV', view.clock);
  for (const id of ['thumbBox', 'vlThumb', 'vlThumbLbl']) $(id).hidden = view.thumb === null;
  setText('vlThumb', view.thumb ?? '');
  const live = view.mode === 'flows';
  setText('camTitleL', live ? 'Raw video' : 'Video on this link');
  setText('camTitleV', live ? 'laptop camera, stand-in for the drone feed' : 'H.264 at the link rate');
}

function showPlaceholder(reason: string) {
  halftone.setVideo(null); videoLink.reset();
  $('camPlaceholder').hidden = false; setText('camReason', reason);
  if (lastSnap) updateRawOverlay(lastSnap); else halftone.live();
}

async function startCamera() {
  const r = { '360': [640, 360], '480': [854, 480], '720': [1280, 720], '1080': [1920, 1080] }[res] ?? [1280, 720];
  if (!navigator.mediaDevices?.getUserMedia) { showPlaceholder(window.isSecureContext ? 'no camera API in this browser' : 'needs https or localhost'); return; }
  try {
    const s = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: r[0] }, height: { ideal: r[1] } }, audio: false });
    if ($('camera').hidden) { s.getTracks().forEach(t => t.stop()); return; }
    stream = s;
    const v = $<HTMLVideoElement>('cam'); v.srcObject = s; v.play().catch(() => {});
    $('camPlaceholder').hidden = true;
    halftone.setVideo(v); videoLink.reset();
    if (lastSnap) updateRawOverlay(lastSnap); else halftone.live();
  } catch (e) {
    showPlaceholder(`${(e as DOMException).name === 'NotAllowedError' ? 'permission refused' : (e as Error).message || 'no camera'}`);
  }
}
function stopCamera() {
  halftone.pause(); halftone.setVideo(null); videoLink.reset();
  stream?.getTracks().forEach(t => t.stop()); stream = null;
  $<HTMLVideoElement>('cam').srcObject = null;
}
/** The panel shows when side-by-side is on or the stage wants it. */
function updateCameraPanel() {
  const want = camWanted || stageOn;
  if (want === !$('camera').hidden) return;
  $('camera').hidden = !want;
  if (want) { updateRawOverlay(lastSnap); startCamera(); } else stopCamera();
}

$('sbs').onclick = () => { camWanted = !camWanted; setToggle('sbs', camWanted); updateCameraPanel(); };
for (const b of buttons('#res button')) b.onclick = () => {
  res = b.dataset.res!;
  for (const o of buttons('#res button')) o.classList.toggle('on', o === b);
  updateRawOverlay(lastSnap);
  const r = { '360': [640, 360], '480': [854, 480], '720': [1280, 720], '1080': [1920, 1080] }[res] ?? [1280, 720];
  stream?.getVideoTracks()[0]?.applyConstraints({ width: { ideal: r[0] }, height: { ideal: r[1] } }).catch(() => {});
};

// ---- STAGE: the presenter view (HACKATHON_PLAN section 5) -------------------------------------
// The operator view keeps the restraint rules; STAGE puts the dramatic visuals on screen: the link
// activity strip moves from DETAILS onto the stage beside the twin, and the video-on-this-link panel
// opens. Remembered per browser; `?stage=1` forces it (e2e screenshots).
const STAGE_KEY = 'minband.stage';
let stageOn = false;
function setStage(on: boolean) {
  stageOn = on; setToggle('stageMode', on); $('stage').classList.toggle('staged', on);
  const act = $('activity');
  if (on) $('stage').insertBefore(act, $('camera')); else $('activitySec').appendChild(act);
  $('activitySec').hidden = on;
  try { localStorage.setItem(STAGE_KEY, on ? '1' : '0'); } catch { /* storage blocked */ }
  updateCameraPanel();
}
$('stageMode').onclick = () => setStage(!stageOn);
try { if (localStorage.getItem(STAGE_KEY) === '1') setStage(true); } catch { /* default: operator view */ }
if (params.has('stage')) setStage(params.get('stage') !== '0');
