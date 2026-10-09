import { TwinScene } from './scene';
import type { ControlMessage, Snapshot } from './types';

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const scene = new TwinScene($('scene'));
const WS_URL = (import.meta.env.VITE_WS_URL as string | undefined) ?? `ws://${location.hostname}:8080`;
let ws: WebSocket;
const send = (m: ControlMessage) => ws?.readyState === 1 && ws.send(JSON.stringify(m));

const history: number[] = [];
const fmtBps = (b: number) => b >= 1e6 ? `${(b / 1e6).toFixed(2)} Mbps` : b >= 1e3 ? `${(b / 1e3).toFixed(1)} kbps` : `${Math.round(b)} bps`;
// Slider stops: log scale from 500 bps to 2 Mbps, 0 = off.
const capValue = (v: number) => v === 0 ? 0 : Math.round(500 * Math.pow(2e6 / 500, (v - 1) / 19));

function drawGraph(snap: Snapshot) {
  const c = $<HTMLCanvasElement>('graph'); const ctx = c.getContext('2d')!;
  c.width = c.clientWidth * devicePixelRatio; c.height = c.clientHeight * devicePixelRatio;
  const total = snap.devices.reduce((a, d) => a + d.bps, 0);
  history.push(total); if (history.length > 300) history.shift();
  const max = 1.6 * Math.max(1000, ...history, snap.shaper.enabled && snap.shaper.bps ? snap.shaper.bps : 0);
  ctx.clearRect(0, 0, c.width, c.height);
  const y = (v: number) => c.height - (Math.log10(1 + v) / Math.log10(1 + max)) * c.height;
  if (snap.shaper.enabled && snap.shaper.bps) { ctx.strokeStyle = '#ff7b72'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(0, y(snap.shaper.bps)); ctx.lineTo(c.width, y(snap.shaper.bps)); ctx.stroke(); ctx.setLineDash([]); }
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 2 * devicePixelRatio; ctx.beginPath();
  history.forEach((v, i) => { const x = i / 299 * c.width; i ? ctx.lineTo(x, y(v)) : ctx.moveTo(x, y(v)); }); ctx.stroke();
}

function render(snap: Snapshot) {
  scene.update(snap);
  const total = snap.devices.reduce((a, d) => a + d.bps, 0);
  $('bps').textContent = fmtBps(total);
  const b = snap.baselines;
  $('baselines').innerHTML = `H.264 720p would be ${fmtBps(b.h264_720p_bps)} (${(b.h264_720p_bps / Math.max(total, 1)).toFixed(0)}× more)<br>naive 30 Hz metadata would be ${fmtBps(b.naiveMetadataBps)}`;
  $('hud').textContent = `${snap.devices.length} device(s)  ${snap.global.length} entities  ${snap.shaper.enabled ? `link: ${snap.shaper.bps ? fmtBps(snap.shaper.bps) : 'uncapped'} ${snap.shaper.delayMs} ms ${Math.round(snap.shaper.loss * 100)}% loss` : 'link: clean'}`;
  $('devices').innerHTML = snap.devices.map(d => `<div class="row"><b>dev ${d.deviceId}</b><span>${fmtBps(d.bps)} · ${d.msgsPerSec.toFixed(1)} msg/s</span></div><div class="baseline">${d.entities.length} ent · kf ${d.stats.keyframes} · Δ ${d.stats.deltas} · gaps ${d.stats.gapsDetected} · nacks ${d.stats.nacksSent}</div>`).join('');
  drawGraph(snap);
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

let impair = false, fusion = true;
const shaper = () => send({ type: 'shaper', config: { enabled: impair, bps: capValue(+$<HTMLInputElement>('capSlider').value), delayMs: +$<HTMLInputElement>('delaySlider').value, loss: +$<HTMLInputElement>('lossSlider').value / 100 } });
$('capSlider').oninput = e => { const v = capValue(+(e.target as HTMLInputElement).value); $('bpsv').textContent = v ? fmtBps(v) : 'off'; shaper(); };
$('delaySlider').oninput = e => { $('delayv').textContent = `${(e.target as HTMLInputElement).value} ms`; shaper(); };
$('lossSlider').oninput = e => { $('lossv').textContent = `${(e.target as HTMLInputElement).value}%`; shaper(); };
$('budgetSlider').oninput = e => { const v = capValue(+(e.target as HTMLInputElement).value); $('budgetv').textContent = v ? fmtBps(v) : 'unlimited'; send({ type: 'budget', bps: v }); };
$('impair').onclick = () => { impair = !impair; $('impair').textContent = `impairment: ${impair ? 'on' : 'off'}`; $('impair').classList.toggle('on', impair); shaper(); };
$('fusion').onclick = () => { fusion = !fusion; $('fusion').textContent = `fusion: ${fusion ? 'on' : 'off'}`; $('fusion').classList.toggle('on', fusion); send({ type: 'fusion', enabled: fusion }); };
$('ghosts').onclick = () => { scene.showGhosts = !scene.showGhosts; $('ghosts').classList.toggle('on', scene.showGhosts); };
