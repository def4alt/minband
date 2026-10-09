import { LINE_STYLES, TwinScene } from './scene';
import { Halftone } from './halftone';
import type { ControlMessage, ShaperConfig, Snapshot } from './types';

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const INK = css('--ink'), INK2 = css('--ink-2'), INK3 = css('--ink-3'), INK4 = css('--ink-4');
const MONO = css('--mono');
const LINK_IN = css('--series-1'), SENT = css('--series-4'); // graph series: colour, not dash

const scene = new TwinScene($('scene'));
const WS_URL = (import.meta.env.VITE_WS_URL as string | undefined) ?? `ws://${location.hostname}:8080`;
// The HTTP API is served by the same server as the WebSocket.
const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? WS_URL.replace(/^ws/, 'http');
let ws: WebSocket;
const send = (m: ControlMessage) => ws?.readyState === 1 && ws.send(JSON.stringify(m));

const fmtBps = (b: number) => b >= 1e6 ? `${(b / 1e6).toFixed(2)} Mbps` : b >= 1e3 ? `${(b / 1e3).toFixed(1)} kbps` : `${Math.round(b)} bps`;
/** Rounded to what a human needs (STYLE.md, Restraint): kbps with one decimal, error in whole cm. */
const fmtKbps = (b: number) => `${(b / 1000).toFixed(1)} kbps`;
const fmtCm = (m: number) => `${Math.round(m * 100)} cm`;
const fmtTimes = (x: number) => `${Math.round(x).toLocaleString('en-US')}×`;
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

// ---- scenarios -------------------------------------------------------------------------------
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
/** Link condition for the credits row: the scenario name, or the shaper settings when custom. */
const linkDesc = (s: ShaperConfig, name: string) => name !== 'custom' ? name : [s.bps ? fmtBps(s.bps) : 'uncapped', `${Math.round(s.delayMs)} ms`, `${Math.round(s.loss * 100)}%`].join(SEP());

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
  // H.264 baseline: a dashed reference when it is on the scale, else a label at the top edge saying how far over
  const h264 = snap.baselines.h264_720p_bps;
  if (h264 <= max) {
    const yy = px(y(h264));
    ctx.strokeStyle = INK2; ctx.setLineDash([1.5, 3]); ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(W, yy); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = INK2; ctx.textAlign = 'left'; ctx.fillText('H.264 720P', 3, yy - 5);
  } else {
    ctx.fillStyle = INK2; ctx.textAlign = 'left';
    ctx.fillText(`▲ H.264 720P ${fmtBps(h264)} · ${fmtTimes(h264 / Math.max(total, 1))} above`, 0, 6);
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
let scenario = 'clean';
function setScenario(name: string) {
  scenario = name;
  for (const b of buttons('#scenario button')) b.classList.toggle('on', b.dataset.scenario === name);
  setText('scenarioName', name);
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
  if (idle('scenario')) setScenario(scenarioOf(s, snap.shaperRevertMs ?? null));
}

// ---- render ----------------------------------------------------------------------------------
let lastTotal = 0;
const swatch = (style: number) => `<svg width="24" height="5" viewBox="0 0 24 5" aria-hidden="true"><line x1="0" y1="2.5" x2="24" y2="2.5" stroke="${INK}" stroke-width="1"${LINE_STYLES[style].svg ? ` stroke-dasharray="${LINE_STYLES[style].svg}"` : ''}/></svg>`;
const detailsOpen = () => !$('details').hidden;

function render(snap: Snapshot) {
  scene.update(snap);
  setStatus(null);
  const total = snap.devices.reduce((a, d) => a + d.bps, 0); lastTotal = total;
  const s = snap.shaper;

  // default view: one primary readout, the twin error, the credits row
  setHTML('bps', `${(total / 1000).toFixed(1)}<small>kbps</small>`); setText('sheetBps', fmtKbps(total));
  const n = snap.devices.length, lost = snap.devices.filter(d => d.silent).length;
  setHTML('cEdge', n ? `${n} device${n === 1 ? '' : 's'}${lost ? `${SEP()}<span class="lost" ${phase()}>${lost} lost</span>` : ''}` : 'no device');
  const name = scenarioOf(s, snap.shaperRevertMs ?? null);
  setHTML('cLink', name === 'blackout' ? `blackout${SEP()}${Math.ceil(snap.shaperRevertMs! / 1000)} s left` : linkDesc(s, name));
  setText('cTwin', `${snap.global.length} entit${snap.global.length === 1 ? 'y' : 'ies'}`);
  syncControls(snap);
  history.push({ total, offered: snap.devices.reduce((a, d) => a + (d.offeredBps ?? d.bps), 0) }); if (history.length > HIST) history.shift();
  if ($('camera').hidden === false) updateRawOverlay();
  if (!detailsOpen()) return;

  // details
  const b = snap.baselines;
  setHTML('baselines', `H.264 720p would be ${fmtBps(b.h264_720p_bps)}, ${fmtTimes(b.h264_720p_bps / Math.max(total, 1))} more<br>naive 30 Hz metadata ${fmtBps(b.naiveMetadataBps)}`);
  $('devices').innerHTML = snap.devices.map(d => {
    const dk = d.key ?? String(d.deviceId), style = scene.styleOf(dk);
    const name = d.provisional ? `Dev ?${SEP()}<span class="note">${esc(d.addr)}</span>` : `Dev ${d.deviceId}`;
    const tags = [d.silent ? `<span class="tag lost" ${phase()}>lost</span>` : '', d.addrChanges ? `<span class="tag">moved ×${d.addrChanges}</span>` : ''].join(' ');
    const sent = s.enabled && d.offeredBps > d.bps * 1.05 ? `<div class="kv"><span class="note">sent before the link</span><span class="note">${fmtBps(d.offeredBps)}</span></div>` : '';
    return `<div class="dev"><div class="kv"><span class="name lbl">${swatch(style)}<span>${name}</span>${tags}</span><span class="val">${fmtBps(d.bps)}</span></div>`
      + `<div class="kv"><span class="note">${d.entities.length} ent${SEP()}${d.msgsPerSec.toFixed(1)} msg/s</span><span class="note dim">${LINE_STYLES[style].name}</span></div>${sent}`
      + `<div class="note dim">kf ${d.stats.keyframes ?? 0}${SEP()}Δ ${d.stats.deltas ?? 0}${SEP()}gaps ${d.stats.gapsDetected ?? 0}${SEP()}nacks ${d.stats.nacksSent ?? 0}</div></div>`;
  }).join('');
  drawGraph(snap);
}

function setStatus(text: string | null) {
  const el = $('status');
  if (text === null) { el.hidden = true; return; }
  el.hidden = false; setText('status', text);
}

function connect() {
  ws = new WebSocket(WS_URL);
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === 'snapshot') render(m.snap);
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
onInput('capSlider', v => { const b = capValue(v); setText('bpsv', b ? fmtBps(b) : 'off'); shaper({ bps: b }); });
onInput('delaySlider', v => { setText('delayv', `${v} ms`); shaper({ delayMs: v }); });
onInput('lossSlider', v => { setText('lossv', `${v}%`); shaper({ loss: v / 100 }); });
onInput('budgetSlider', v => { const b = capValue(v); setText('budgetv', b ? fmtBps(b) : 'unlimited'); send({ type: 'budget', bps: b }); });
$('impair').onclick = () => { const on = !isOn('impair'); setToggle('impair', on); shaper({ enabled: on }); };
$('fusion').onclick = () => { const on = !isOn('fusion'); setToggle('fusion', on); send({ type: 'fusion', enabled: on }); };
$('ghosts').onclick = () => { scene.showGhosts = !scene.showGhosts; setToggle('ghosts', scene.showGhosts); };
for (const b of buttons('#scenario button')) b.onclick = () => {
  touch('scenario');
  const v = b.dataset.scenario!;
  setScenario(v);
  // The server holds the blackout timer and restores the previous link itself (survives a reload).
  if (v === 'blackout') send({ type: 'shaper', config: { enabled: true, loss: 1 }, revertAfterMs: BLACKOUT_MS });
  else if (SCENARIOS[v]) shaper(SCENARIOS[v]);
};

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

// ---- side-by-side: twin lines vs halftoned raw video -----------------------------------------
const RES: Record<string, { w: number; h: number; bps: number }> = {
  '360': { w: 640, h: 360, bps: 250_000 },
  '480': { w: 854, h: 480, bps: 500_000 },
  '720': { w: 1280, h: 720, bps: 1_500_000 },
  '1080': { w: 1920, h: 1080, bps: 3_000_000 },
};
let res = '720';
let stream: MediaStream | null = null;
let camWanted = false;
const halftone = new Halftone($<HTMLCanvasElement>('halftone'));

function updateRawOverlay() {
  const r = RES[res];
  setText('rawRate', fmtBps(r.bps));
  setText('rawVs', lastTotal > 0 ? `${fmtKbps(lastTotal)}${' · '}${fmtTimes(r.bps / lastTotal)} less` : 'idle');
}

function showPlaceholder(reason: string) {
  halftone.still();
  $('camPlaceholder').hidden = false; setText('camReason', reason);
}

async function startCamera() {
  const r = RES[res];
  if (!navigator.mediaDevices?.getUserMedia) { showPlaceholder(window.isSecureContext ? 'no camera API in this browser' : 'needs https or localhost'); return; }
  try {
    const s = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: r.w }, height: { ideal: r.h } }, audio: false });
    if (!camWanted) { s.getTracks().forEach(t => t.stop()); return; }
    stream = s;
    const v = $<HTMLVideoElement>('cam'); v.srcObject = s; v.play().catch(() => {});
    $('camPlaceholder').hidden = true;
    halftone.play(v);
  } catch (e) {
    showPlaceholder(`${(e as DOMException).name === 'NotAllowedError' ? 'permission refused' : (e as Error).message || 'no camera'}`);
  }
}
function stopCamera() {
  halftone.stop();
  stream?.getTracks().forEach(t => t.stop()); stream = null;
  $<HTMLVideoElement>('cam').srcObject = null;
}

$('sbs').onclick = () => {
  camWanted = !camWanted;
  setToggle('sbs', camWanted);
  $('camera').hidden = !camWanted;
  updateRawOverlay();
  if (camWanted) startCamera(); else stopCamera();
};
for (const b of buttons('#res button')) b.onclick = () => {
  res = b.dataset.res!;
  for (const o of buttons('#res button')) o.classList.toggle('on', o === b);
  updateRawOverlay();
  const r = RES[res];
  stream?.getVideoTracks()[0]?.applyConstraints({ width: { ideal: r.w }, height: { ideal: r.h } }).catch(() => {});
};
