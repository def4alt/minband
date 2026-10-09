import { TwinScene } from './scene';
import type { ControlMessage, ShaperConfig, Snapshot } from './types';

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const scene = new TwinScene($('scene'));
const WS_URL = (import.meta.env.VITE_WS_URL as string | undefined) ?? `ws://${location.hostname}:8080`;
// The HTTP API is served by the same server as the WebSocket.
const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? WS_URL.replace(/^ws/, 'http');
let ws: WebSocket;
const send = (m: ControlMessage) => ws?.readyState === 1 && ws.send(JSON.stringify(m));

const fmtBps = (b: number) => b >= 1e6 ? `${(b / 1e6).toFixed(2)} Mbps` : b >= 1e3 ? `${(b / 1e3).toFixed(1)} kbps` : `${Math.round(b)} bps`;
const fmtM = (m: number) => m < 1 ? `${(m * 100).toFixed(1)} cm` : `${m.toFixed(2)} m`;
// Slider stops: log scale from 500 bps to 2 Mbps, 0 = off.
const STEPS = 200, LO = 500, HI = 2e6;
const capValue = (v: number) => v === 0 ? 0 : Math.round(LO * Math.pow(HI / LO, (v - 1) / (STEPS - 1)));
const capSlider = (bps: number) => bps <= 0 ? 0 : Math.max(1, Math.min(STEPS, Math.round(1 + (STEPS - 1) * Math.log(bps / LO) / Math.log(HI / LO))));

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

// ---- graph -----------------------------------------------------------------------------------
const history: { total: number; offered: number }[] = [];
function drawGraph(snap: Snapshot) {
  const c = $<HTMLCanvasElement>('graph'); const ctx = c.getContext('2d')!;
  c.width = c.clientWidth * devicePixelRatio; c.height = c.clientHeight * devicePixelRatio;
  const total = snap.devices.reduce((a, d) => a + d.bps, 0);
  const offered = snap.devices.reduce((a, d) => a + (d.offeredBps ?? d.bps), 0);
  history.push({ total, offered }); if (history.length > 300) history.shift();
  const max = 1.6 * Math.max(1000, ...history.map(h => Math.max(h.total, h.offered)), snap.shaper.enabled && snap.shaper.bps ? snap.shaper.bps : 0);
  ctx.clearRect(0, 0, c.width, c.height);
  const y = (v: number) => c.height - (Math.log10(1 + v) / Math.log10(1 + max)) * c.height;
  if (snap.shaper.enabled && snap.shaper.bps) { ctx.strokeStyle = '#ff7b72'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(0, y(snap.shaper.bps)); ctx.lineTo(c.width, y(snap.shaper.bps)); ctx.stroke(); ctx.setLineDash([]); }
  const line = (pick: (h: { total: number; offered: number }) => number, color: string, w: number) => {
    ctx.strokeStyle = color; ctx.lineWidth = w * devicePixelRatio; ctx.beginPath();
    history.forEach((h, i) => { const x = i / 299 * c.width; i ? ctx.lineTo(x, y(pick(h))) : ctx.moveTo(x, y(pick(h))); }); ctx.stroke();
  };
  if (snap.shaper.enabled) line(h => h.offered, '#8b949e88', 1); // what the edge sent, before the link
  line(h => h.total, '#58a6ff', 2);
}

// ---- controls synced from the server ---------------------------------------------------------
// Sliders reflect the server's shaper (it may be driven from /api/shaper or another viewer),
// except while the operator is touching one.
const touched = new Map<string, number>();
const touch = (id: string) => touched.set(id, performance.now());
const idle = (id: string) => performance.now() - (touched.get(id) ?? -1e9) > 1500;
const setSlider = (id: string, v: number) => { if (idle(id)) $<HTMLInputElement>(id).value = String(v); };

function syncControls(snap: Snapshot) {
  const s = snap.shaper;
  setSlider('capSlider', capSlider(s.bps)); $('bpsv').textContent = s.bps ? fmtBps(s.bps) : 'off';
  setSlider('delaySlider', s.delayMs); $('delayv').textContent = `${Math.round(s.delayMs)} ms`;
  setSlider('lossSlider', Math.round(s.loss * 100)); $('lossv').textContent = `${Math.round(s.loss * 100)}%`;
  const budget = snap.budgetBps ?? 0;
  setSlider('budgetSlider', capSlider(budget)); $('budgetv').textContent = budget ? fmtBps(budget) : 'unlimited';
  $('impair').textContent = `impairment: ${s.enabled ? 'on' : 'off'}`; $('impair').classList.toggle('on', s.enabled);
  $('fusion').textContent = `fusion: ${snap.fusion ? 'on' : 'off'}`; $('fusion').classList.toggle('on', snap.fusion);
  if (idle('scenario')) $<HTMLSelectElement>('scenario').value = scenarioOf(s, snap.shaperRevertMs ?? null);
}

// ---- render ----------------------------------------------------------------------------------
let lastTotal = 0;
function render(snap: Snapshot) {
  scene.update(snap);
  const total = snap.devices.reduce((a, d) => a + d.bps, 0); lastTotal = total;
  $('bps').textContent = fmtBps(total); $('sheetBps').textContent = fmtBps(total);
  const b = snap.baselines;
  $('baselines').innerHTML = `H.264 720p would be ${fmtBps(b.h264_720p_bps)} (${(b.h264_720p_bps / Math.max(total, 1)).toFixed(0)}× more)<br>naive 30 Hz metadata would be ${fmtBps(b.naiveMetadataBps)}`;
  const s = snap.shaper;
  const link = s.enabled ? `link: ${s.bps ? fmtBps(s.bps) : 'uncapped'} ${s.delayMs} ms ${Math.round(s.loss * 100)}% loss` : 'link: clean';
  const blackout = snap.shaperRevertMs != null ? ` <span class="alert">BLACKOUT ${Math.ceil(snap.shaperRevertMs / 1000)} s</span>` : '';
  const stale = snap.global.filter(g => g.stale).length;
  $('hud').innerHTML = `${snap.devices.length} device(s) · ${snap.global.length} entities${stale ? ` (${stale} stale)` : ''} · ${link}${blackout}`;
  $('devices').innerHTML = snap.devices.map(d => {
    const name = d.provisional ? `dev ? <span class="tag">${d.addr}</span>` : `dev ${d.deviceId}`;
    const tags = [d.silent ? '<span class="tag warn">silent</span>' : '', d.addrChanges ? `<span class="tag">moved ×${d.addrChanges}</span>` : ''].join(' ');
    const offered = s.enabled && d.offeredBps > d.bps * 1.05 ? ` <span class="baseline">(sent ${fmtBps(d.offeredBps)})</span>` : '';
    return `<div class="dev"><div class="row"><b>${name} ${tags}</b><span>${fmtBps(d.bps)}${offered} · ${d.msgsPerSec.toFixed(1)} msg/s</span></div><div class="baseline">${d.entities.length} ent · kf ${d.stats.keyframes} · Δ ${d.stats.deltas} · gaps ${d.stats.gapsDetected} · nacks ${d.stats.nacksSent}</div></div>`;
  }).join('');
  syncControls(snap);
  drawGraph(snap);
  updateRawOverlay();
}

function connect() {
  ws = new WebSocket(WS_URL);
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === 'snapshot') render(m.snap);
    else if (m.type === 'log' && m.lines.length) { const l = $('log'); l.textContent = (l.textContent + m.lines.join('\n') + '\n').split('\n').slice(-60).join('\n'); l.scrollTop = l.scrollHeight; }
  };
  ws.onclose = () => { $('hud').textContent = 'disconnected, retrying…'; setTimeout(connect, 1000); };
}
connect();

// ---- twin error (HTTP, every 2 s) ------------------------------------------------------------
interface MetricsLite { twinError: { meanM: number | null; p95M: number | null; samples: number }; devices: { twinError: { updatedMs: number } | null }[]; t: number }
async function pollMetrics() {
  try {
    const m = await (await fetch(`${API_URL}/api/metrics`, { cache: 'no-store' })).json() as MetricsLite;
    const te = m.twinError;
    if (te.meanM === null) {
      $('twinErr').textContent = '–'; $('twinErrDetail').textContent = 'no ground truth uploaded'; $('sheetErr').textContent = 'controls ▴';
    } else {
      const updated = Math.max(...m.devices.map(d => d.twinError?.updatedMs ?? 0));
      const age = updated ? Math.max(0, Math.round((m.t - updated) / 1000)) : null;
      $('twinErr').textContent = fmtM(te.meanM);
      $('twinErrDetail').textContent = `mean · p95 ${fmtM(te.p95M!)} · ${te.samples} GT rows${age !== null ? ` · ${age} s ago` : ''}`;
      $('sheetErr').textContent = `err ${fmtM(te.meanM)} ▴`;
    }
  } catch {
    $('twinErrDetail').textContent = 'metrics unavailable';
  }
}
pollMetrics(); setInterval(pollMetrics, 2000);

// ---- operator controls -----------------------------------------------------------------------
const shaper = (config: Partial<ShaperConfig>) => send({ type: 'shaper', config });
const onInput = (id: string, fn: (v: number) => void) => { $(id).oninput = e => { touch(id); fn(+(e.target as HTMLInputElement).value); }; };
onInput('capSlider', v => { const b = capValue(v); $('bpsv').textContent = b ? fmtBps(b) : 'off'; shaper({ bps: b }); });
onInput('delaySlider', v => { $('delayv').textContent = `${v} ms`; shaper({ delayMs: v }); });
onInput('lossSlider', v => { $('lossv').textContent = `${v}%`; shaper({ loss: v / 100 }); });
onInput('budgetSlider', v => { const b = capValue(v); $('budgetv').textContent = b ? fmtBps(b) : 'unlimited'; send({ type: 'budget', bps: b }); });
$('impair').onclick = () => { const on = !$('impair').classList.contains('on'); $('impair').classList.toggle('on', on); $('impair').textContent = `impairment: ${on ? 'on' : 'off'}`; shaper({ enabled: on }); };
$('fusion').onclick = () => { const on = !$('fusion').classList.contains('on'); $('fusion').classList.toggle('on', on); $('fusion').textContent = `fusion: ${on ? 'on' : 'off'}`; send({ type: 'fusion', enabled: on }); };
$('ghosts').onclick = () => { scene.showGhosts = !scene.showGhosts; $('ghosts').classList.toggle('on', scene.showGhosts); };
$<HTMLSelectElement>('scenario').onchange = e => {
  touch('scenario');
  const v = (e.target as HTMLSelectElement).value;
  // The server holds the blackout timer and restores the previous link itself (survives a reload).
  if (v === 'blackout') send({ type: 'shaper', config: { enabled: true, loss: 1 }, revertAfterMs: BLACKOUT_MS });
  else if (SCENARIOS[v]) shaper(SCENARIOS[v]);
};

// ---- bottom sheet (narrow screens) -----------------------------------------------------------
$('sheetToggle').onclick = () => {
  const open = $('side').classList.toggle('open');
  $('sheetToggle').setAttribute('aria-expanded', String(open));
};

// ---- side-by-side: twin vs raw video ---------------------------------------------------------
const RES: Record<string, { w: number; h: number; bps: number }> = {
  '360': { w: 640, h: 360, bps: 250_000 },
  '480': { w: 854, h: 480, bps: 500_000 },
  '720': { w: 1280, h: 720, bps: 1_500_000 },
  '1080': { w: 1920, h: 1080, bps: 3_000_000 },
};
let stream: MediaStream | null = null;
let camWanted = false;

function updateRawOverlay() {
  const r = RES[$<HTMLSelectElement>('res').value];
  $('rawRate').textContent = fmtBps(r.bps);
  $('rawVs').textContent = lastTotal > 0 ? `twin right now: ${fmtBps(lastTotal)} (${Math.round(r.bps / lastTotal).toLocaleString()}× less)` : 'twin right now: idle';
}

function showPlaceholder(reason: string) {
  $<HTMLVideoElement>('cam').hidden = true;
  $('camPlaceholder').hidden = false; $('camReason').textContent = reason;
}

async function startCamera() {
  const r = RES[$<HTMLSelectElement>('res').value];
  if (!navigator.mediaDevices?.getUserMedia) { showPlaceholder(window.isSecureContext ? 'no camera API in this browser' : 'needs https or localhost'); return; }
  try {
    const s = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: r.w }, height: { ideal: r.h } }, audio: false });
    if (!camWanted) { s.getTracks().forEach(t => t.stop()); return; }
    stream = s;
    const v = $<HTMLVideoElement>('cam'); v.srcObject = s; v.hidden = false; $('camPlaceholder').hidden = true;
  } catch (e) {
    showPlaceholder(`${(e as DOMException).name === 'NotAllowedError' ? 'permission refused' : (e as Error).message || 'no camera'}`);
  }
}
function stopCamera() {
  stream?.getTracks().forEach(t => t.stop()); stream = null;
  $<HTMLVideoElement>('cam').srcObject = null;
}

$('sbs').onclick = () => {
  camWanted = !camWanted;
  $('sbs').classList.toggle('on', camWanted);
  $('camera').hidden = !camWanted;
  updateRawOverlay();
  if (camWanted) startCamera(); else stopCamera();
};
$<HTMLSelectElement>('res').onchange = () => {
  updateRawOverlay();
  const r = RES[$<HTMLSelectElement>('res').value];
  stream?.getVideoTracks()[0]?.applyConstraints({ width: { ideal: r.w }, height: { ideal: r.h } }).catch(() => {});
};
