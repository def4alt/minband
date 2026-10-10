// Side-by-side page: left = clip + edge truth; right = what the receiver decoded from the wire.
// No build step; talks the WebSocket contract in README.md.
import { loadCamera } from './camera.js';

const $ = (id) => document.getElementById(id);
const video = $('video'), overlay = $('overlay'), hud = $('hud'), topdown = $('topdown'), menu = $('menu');
const eventList = $('eventList'), frameList = $('frameList'), spark = $('spark'), readout = $('readout');
const seek = $('seek'), timeEl = $('time'), playBtn = $('playBtn'), conn = $('conn');

const CLS = { dismount: '#ffd60a', vehicle: '#4cc9f0', armour: '#ff4d4d', other: '#b0b8c4' };
const classGroup = (cls) => (cls === 0 ? 'dismount' : cls === 101 ? 'armour' : cls === 100 ? 'other' : 'vehicle');
const dominant = (mix) => Object.entries(mix || {}).sort((a, b) => b[1] - a[1])[0]?.[0] || 'other';
const mixLabel = (mix) => Object.entries(mix || {}).filter(([, v]) => v > 0).map(([k, v]) => `${v}${{ dismount: 'd', vehicle: 'v', armour: 'a', other: 'o' }[k]}`).join(' ');
const LIVENESS_ALPHA = { fresh: 1, unheard: 0.6, lost: 0.35, 'out of view': 0.45, departed: 0.3 };

const state = {
  cam: null, msg: null, prevT: -1, ws: null, dragging: false, lastSeekSent: 0,
  events: [], frames: [], bps: [], focusedId: null, playing: true, rate: 1, profile: 'lora', lastProfile: 'lora',
  menuId: null, inferT: [],
};

// ---- websocket ---------------------------------------------------------------------------------
function send(obj) { if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(obj)); }
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/`);
  state.ws = ws;
  ws.onopen = () => { conn.className = 'ok'; conn.title = 'connected'; };
  ws.onclose = () => { conn.className = ''; conn.title = 'disconnected'; setTimeout(connect, 1000); };
  ws.onmessage = (ev) => { try { onMessage(JSON.parse(ev.data)); } catch (e) { console.error(e); } };
}

function onMessage(m) {
  if (state.msg && m.t < state.prevT - 0.5) resetLogs();
  state.prevT = m.t;
  state.msg = m;
  if (m.duration && Number($('seek').max) !== m.duration) $('seek').max = m.duration;
  // play/pause + rate: from the (optional) top-level fields, else inferred from t
  state.inferT.push(m.t); if (state.inferT.length > 4) state.inferT.shift();
  const inferred = state.inferT.length >= 3 ? state.inferT[state.inferT.length - 1] > state.inferT[0] : true;
  state.playing = typeof m.playing === 'boolean' ? m.playing : inferred;
  if (typeof m.rate === 'number') state.rate = m.rate;
  state.profile = m.wire?.profile || state.profile;
  if (state.profile !== 'blackout') state.lastProfile = state.profile;
  state.focusedId = (m.edge?.contacts || []).find((c) => c.focused)?.id ?? null;
  // logs
  for (const e of m.rx?.events || []) pushEvent(e);
  for (const f of m.wire?.frames || []) pushFrame(f);
  state.bps.push({ t: m.t, bps: m.wire?.bytesPerS ?? 0 });
  while (state.bps.length && m.t - state.bps[0].t > 60) state.bps.shift();
  syncVideo(m);
  updateControls(m);
  drawSpark();
  const w = m.wire || {}, r = m.rx || {};
  const d = w.detail;
  readout.innerHTML = `budget <b>${w.budgetBps ?? '?'} bit/s</b> · <b>${(w.bytesPerS ?? 0).toFixed(1)} B/s</b> · <b>${r.known ?? 0}</b> of <b>${r.of ?? 0}</b> known · <b>${w.profile ?? '?'}</b>${w.up === false ? ' <span class="lost">LINK DOWN</span>' : ''} · ${r.bytesTotal ?? 0} B total · ${w.dropped ?? 0} dropped`
    + (d ? `<br>detail <b>L${d.level}</b> · groups at <b>${d.linkM} m</b> · backlog ${d.backlogS.toFixed(1)} s · ${d.changes} change${d.changes === 1 ? '' : 's'}` : '');
  hud.textContent = `t=${m.t.toFixed(1)} s · edge ${m.edge?.tracks?.length ?? 0} tracks / ${m.edge?.contacts?.length ?? 0} contacts${d ? ` (L${d.level}, ${d.linkM} m)` : ''} · rx ${r.contacts?.length ?? 0} contacts${r.ego ? ` · ego age ${r.ego.ageS.toFixed(1)} s` : ' · no ego yet'}${r.ego?.groupM ? ` · grouped at ${r.ego.groupM} m` : ''}`;
}

function resetLogs() {
  state.events = []; state.frames = []; state.bps = []; eventList.textContent = ''; frameList.textContent = '';
}

function syncVideo(m) {
  const target = m.clipT ?? m.t;
  if (Math.abs(video.currentTime - target) > 0.25 && video.readyState >= 1) video.currentTime = target;
  if (video.playbackRate !== state.rate) video.playbackRate = state.rate;
  if (state.playing && video.paused) video.play().catch(() => {});
  else if (!state.playing && !video.paused) video.pause();
}

function updateControls(m) {
  playBtn.textContent = state.playing ? '❚❚ pause' : '▶ play';
  if (!state.dragging) seek.value = String(m.clipT ?? m.t);
  timeEl.textContent = `t=${m.t.toFixed(1)} s · ${state.rate}×`;
  for (const b of document.querySelectorAll('#rates button')) b.classList.toggle('on', Number(b.dataset.rate) === state.rate);
  for (const b of document.querySelectorAll('#profiles button')) b.classList.toggle('on', b.dataset.profile === state.profile);
}

// ---- events + wire log -------------------------------------------------------------------------
function pushEvent(e) {
  state.events.push(e);
  const div = document.createElement('div');
  div.innerHTML = `<span class="t">t=${Number(e.t).toFixed(1)}</span> <span class="k k-${e.kind}">[${e.kind}]</span> ${escapeHtml(e.text)}`;
  div.className = `k-${e.kind}`;
  eventList.appendChild(div);
  while (state.events.length > 200) { state.events.shift(); eventList.firstChild?.remove(); }
  $('eventCount').textContent = `${state.events.length}`;
  eventList.scrollTop = eventList.scrollHeight;
}
function pushFrame(f) {
  state.frames.push(f);
  const d = document.createElement('details');
  const kinds = {};
  for (const l of f.lines || []) { const k = l.split(' ')[0]; kinds[k] = (kinds[k] || 0) + 1; }
  const summary = Object.entries(kinds).map(([k, n]) => (n > 1 ? `${k} ×${n}` : k)).join(', ');
  d.innerHTML = `<summary>seq ${f.seq} · ${f.bytes} B · <span class="${f.delivered ? 'ok' : 'lost'}">${f.delivered ? 'delivered' : 'lost'}</span> · ${escapeHtml(summary)}</summary><pre>${escapeHtml((f.lines || []).join('\n'))}</pre>`;
  frameList.appendChild(d);
  while (state.frames.length > 200) { state.frames.shift(); frameList.firstChild?.remove(); }
  $('wireCount').textContent = `${state.frames.length} frames`;
  frameList.scrollTop = frameList.scrollHeight;
}
const escapeHtml = (s) => String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

function drawSpark() {
  const ctx = spark.getContext('2d'), W = spark.width, H = spark.height;
  ctx.clearRect(0, 0, W, H);
  if (!state.bps.length) return;
  const t1 = state.bps[state.bps.length - 1].t, t0 = t1 - 60;
  const max = Math.max(10, ...state.bps.map((s) => s.bps));
  const budgetB = (state.msg?.wire?.budgetBps || 0) / 8;
  if (budgetB > 0 && budgetB <= max * 1.5) {
    const y = H - 2 - (Math.min(budgetB, max) / max) * (H - 4);
    ctx.strokeStyle = '#d29922'; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); ctx.setLineDash([]);
  }
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1.2; ctx.beginPath();
  state.bps.forEach((s, i) => {
    const x = ((s.t - t0) / 60) * W, y = H - 2 - (s.bps / max) * (H - 4);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = '#8a93a3'; ctx.font = '9px monospace'; ctx.fillText(`${max.toFixed(0)} B/s`, 2, 9);
}

// ---- video overlay -----------------------------------------------------------------------------
function fitCanvas(canvas, w, h) {
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  canvas.style.width = `${w}px`; canvas.style.height = `${h}px`;
  const ctx = canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); return ctx;
}

function drawOverlay() {
  const m = state.msg, cam = state.cam;
  const rect = video.getBoundingClientRect(), wrap = video.parentElement.getBoundingClientRect();
  overlay.style.left = `${rect.left - wrap.left - video.parentElement.clientLeft}px`; overlay.style.top = `${rect.top - wrap.top - video.parentElement.clientTop}px`;
  const ctx = fitCanvas(overlay, rect.width, rect.height);
  ctx.clearRect(0, 0, rect.width, rect.height);
  if (!m || !cam || !rect.width) return;
  const sx = rect.width / cam.width, sy = rect.height / cam.height;
  const clipT = video.readyState >= 1 ? video.currentTime : m.clipT;
  const dt = Math.max(-0.3, Math.min(0.3, clipT - m.t)); // dead-reckon tracks between messages
  const P = (e, n) => { const p = cam.projectEN(e, n, clipT); return p ? { x: p.u * sx, y: p.v * sy } : null; };
  // A ground radius in pixels: the median over four directions, capped at half the frame, so a
  // point near the camera's horizon (where perspective stretches metres into hundreds of pixels,
  // or flips them) cannot balloon the ring.
  const RW = rect.width, RH = rect.height, MARGIN = 12;
  const radiusPx = (e, n, r, c0) => {
    const ds = [[r, 0], [-r, 0], [0, r], [0, -r]].map(([de, dn]) => P(e + de, n + dn)).filter(Boolean).map((p) => Math.hypot(p.x - c0.x, p.y - c0.y)).sort((a, b) => a - b);
    return ds.length ? Math.max(3, Math.min(ds[ds.length >> 1], 0.5 * Math.min(RW, RH))) : 3;
  };
  const inFrame = (p) => p && Number.isFinite(p.x) && Number.isFinite(p.y) && p.x >= -MARGIN && p.x <= RW + MARGIN && p.y >= -MARGIN && p.y <= RH + MARGIN;
  // Out of the frame: no rings, a small marker on the border pointing to where it went.
  const offFrame = (p, col, label) => {
    const cx = RW / 2, cy = RH / 2, dx = p.x - cx, dy = p.y - cy;
    const k = Math.min(Math.abs((RW / 2 - 8) / (dx || 1e-9)), Math.abs((RH / 2 - 8) / (dy || 1e-9)));
    const bx = cx + dx * k, by = cy + dy * k, a = Math.atan2(dy, dx);
    ctx.save(); ctx.globalAlpha = 0.85; ctx.fillStyle = col; ctx.translate(bx, by); ctx.rotate(a);
    ctx.beginPath(); ctx.moveTo(7, 0); ctx.lineTo(-5, -5); ctx.lineTo(-5, 5); ctx.closePath(); ctx.fill(); ctx.restore();
    ctx.globalAlpha = 0.85; ctx.fillStyle = col; ctx.textAlign = bx > RW / 2 ? 'right' : 'left'; ctx.textBaseline = by > RH / 2 ? 'bottom' : 'top';
    ctx.fillText(label, bx + (bx > RW / 2 ? -10 : 10), by + (by > RH / 2 ? -6 : 6));
  };
  ctx.font = '11px ui-monospace, Menlo, monospace'; ctx.textBaseline = 'bottom';
  if ($('showEdgeContacts').checked) {
    for (const c of m.edge?.contacts || []) {
      const p = P(c.e, c.n); if (!p) continue;
      if (!inFrame(p)) continue; // the receiver's marker below shows where it went
      const r = radiusPx(c.e, c.n, c.radius, p);
      const col = CLS[dominant(c.mix)];
      ctx.globalAlpha = c.lost ? 0.35 : c.confirmed ? 0.9 : 0.5;
      ctx.lineWidth = c.focused ? 3 : 1.5; ctx.strokeStyle = c.focused ? '#ffd166' : col;
      ctx.setLineDash(c.confirmed ? [] : [4, 4]);
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
      const label = `#${c.id} ${c.count} ${mixLabel(c.mix)}${c.motion === 'moving' ? ' →' : ''}`;
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = 'rgba(0,0,0,.6)'; ctx.fillRect(p.x - tw / 2 - 3, p.y - r - 15, tw + 6, 14);
      ctx.fillStyle = c.focused ? '#ffd166' : col; ctx.textAlign = 'center'; ctx.fillText(label, p.x, p.y - r - 3);
      if (c.motion === 'moving' && c.speed > 0.2) {
        const q = P(c.e + Math.sin((c.course * Math.PI) / 180) * c.speed * 3, c.n + Math.cos((c.course * Math.PI) / 180) * c.speed * 3);
        if (q) { ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke(); }
      }
    }
  }
  if ($('showTracks').checked) {
    for (const tr of m.edge?.tracks || []) {
      const p = P(tr.e + (tr.ve || 0) * dt, tr.n + (tr.vn || 0) * dt); if (!p) continue;
      ctx.globalAlpha = 0.95; ctx.fillStyle = CLS[classGroup(tr.cls)];
      ctx.beginPath(); ctx.arc(p.x, p.y, 3.2, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,.7)'; ctx.lineWidth = 1; ctx.stroke();
    }
  }
  if ($('showRxOnVideo').checked) {
    for (const c of m.rx?.contacts || []) {
      if (c.departed || c.child) continue;
      const p = P(c.e, c.n); if (!p) continue;
      if (c.located !== false && !inFrame(p)) { offFrame(p, '#ff7eb6', `rx#${c.id} ${c.liveness === 'fresh' ? 'off frame' : c.liveness}`); continue; }
      if (c.located === false) {
        const q = P(c.seenE ?? c.e, c.seenN ?? c.n); if (!q || !inFrame(q)) continue;
        ctx.globalAlpha = 0.6; ctx.fillStyle = '#ff7eb6'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.fillText(`rx#${c.id} last seen here ${c.ageS.toFixed(0)} s ago`, q.x, q.y + 3); continue;
      }
      ctx.globalAlpha = LIVENESS_ALPHA[c.liveness] ?? 0.6;
      ctx.strokeStyle = '#ff7eb6'; ctx.lineWidth = 1.5; ctx.setLineDash([6, 4]);
      ctx.beginPath(); ctx.arc(p.x, p.y, radiusPx(c.e, c.n, c.radius, p), 0, Math.PI * 2); ctx.stroke();
      { // the 95 % circle (PROTOCOL.md 5.3)
        ctx.setLineDash([2, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(p.x, p.y, radiusPx(c.e, c.n, c.radius + (c.ceShown ?? c.ce ?? 0), p), 0, Math.PI * 2); ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.fillStyle = '#ff7eb6'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      ctx.fillText(`rx#${c.id} ${c.count} ${c.liveness}${c.ageS > 2 ? ` ${c.ageS.toFixed(0)}s` : ''}`, p.x, p.y + 3);
      ctx.textBaseline = 'bottom';
    }
  }
  ctx.globalAlpha = 1;
}

// ---- top-down ----------------------------------------------------------------------------------
const view = { cx: 0, cy: 0, ppm: 2 };
function topdownTransform(W, H) {
  const m = state.msg, ego = m?.edge?.ego || m?.rx?.ego;
  const fpR = ego?.fpRadius || 80;
  view.cx = ego ? ego.fpE : 0; view.cy = ego ? ego.fpN : 0;
  view.ppm = Math.min(W, H) / (2 * fpR * 1.25);
  return { toPx: (e, n) => ({ x: W / 2 + (e - view.cx) * view.ppm, y: H / 2 - (n - view.cy) * view.ppm }), toEN: (x, y) => ({ e: view.cx + (x - W / 2) / view.ppm, n: view.cy - (y - H / 2) / view.ppm }) };
}
function drawTopdown() {
  const rect = topdown.getBoundingClientRect(), W = rect.width, H = rect.height;
  const ctx = fitCanvas(topdown, W, H);
  ctx.fillStyle = '#0b0e13'; ctx.fillRect(0, 0, W, H);
  const m = state.msg; if (!m) return;
  const { toPx } = topdownTransform(W, H);
  // grid every 50 m
  const e0 = view.cx - W / 2 / view.ppm, e1 = view.cx + W / 2 / view.ppm, n0 = view.cy - H / 2 / view.ppm, n1 = view.cy + H / 2 / view.ppm;
  ctx.strokeStyle = '#1f2633'; ctx.lineWidth = 1; ctx.fillStyle = '#4a5261'; ctx.font = '10px ui-monospace, Menlo, monospace'; ctx.textAlign = 'left'; ctx.textBaseline = 'top';
  for (let e = Math.ceil(e0 / 50) * 50; e <= e1; e += 50) { const p = toPx(e, 0); ctx.beginPath(); ctx.moveTo(p.x, 0); ctx.lineTo(p.x, H); ctx.stroke(); ctx.fillText(`${e} m`, p.x + 2, 2); }
  for (let n = Math.ceil(n0 / 50) * 50; n <= n1; n += 50) { const p = toPx(0, n); ctx.beginPath(); ctx.moveTo(0, p.y); ctx.lineTo(W, p.y); ctx.stroke(); ctx.fillText(`${n} m`, 2, p.y + 2); }
  // north arrow
  ctx.strokeStyle = '#8a93a3'; ctx.fillStyle = '#8a93a3'; ctx.beginPath(); ctx.moveTo(W - 16, 30); ctx.lineTo(W - 16, 10); ctx.lineTo(W - 20, 16); ctx.moveTo(W - 16, 10); ctx.lineTo(W - 12, 16); ctx.stroke(); ctx.fillText('N', W - 20, 32);
  // footprint + drone
  const ego = m.edge?.ego, rxEgo = m.rx?.ego;
  const egoShown = rxEgo || ego;
  if (egoShown) {
    const fp = toPx(egoShown.fpE, egoShown.fpN);
    ctx.strokeStyle = 'rgba(88,166,255,.5)'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.arc(fp.x, fp.y, egoShown.fpRadius * view.ppm, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
    const d = toPx(egoShown.e, egoShown.n), a = ((egoShown.heading || 0) * Math.PI) / 180;
    ctx.save(); ctx.translate(d.x, d.y); ctx.rotate(a);
    ctx.fillStyle = rxEgo ? '#58a6ff' : 'rgba(88,166,255,.4)'; ctx.beginPath(); ctx.moveTo(0, -10); ctx.lineTo(7, 8); ctx.lineTo(0, 4); ctx.lineTo(-7, 8); ctx.closePath(); ctx.fill();
    ctx.restore();
    ctx.fillStyle = '#58a6ff'; ctx.textAlign = 'left';
    ctx.fillText(`ego ${egoShown.nav} ${egoShown.altAgl} m${rxEgo ? ` · ${rxEgo.ageS.toFixed(0)} s` : ' (not yet heard)'}`, d.x + 12, d.y - 6);
  }
  // edge ghosts
  if ($('showGhosts').checked) {
    ctx.globalAlpha = 0.35;
    for (const tr of m.edge?.tracks || []) { const p = toPx(tr.e, tr.n); ctx.fillStyle = CLS[classGroup(tr.cls)]; ctx.fillRect(p.x - 1, p.y - 1, 2, 2); }
    for (const c of m.edge?.contacts || []) {
      if (c.lost) continue;
      const p = toPx(c.e, c.n); ctx.strokeStyle = '#ffd166'; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.arc(p.x, p.y, c.radius * view.ppm, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
    }
    ctx.globalAlpha = 1;
  }
  // rx contacts
  ctx.font = '11px ui-monospace, Menlo, monospace'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  for (const c of m.rx?.contacts || []) {
    if (c.departed || c.child) continue; // tombstones stay in the log, children are drawn by their parent's split
    if (c.located === false) { // the circle would be wider than the camera footprint: show where it was last seen
      const q = toPx(c.seenE ?? c.e, c.seenN ?? c.n), col = CLS[dominant(c.mix)];
      ctx.globalAlpha = 0.6; ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(q.x - 5, q.y - 5); ctx.lineTo(q.x + 5, q.y + 5); ctx.moveTo(q.x + 5, q.y - 5); ctx.lineTo(q.x - 5, q.y + 5); ctx.stroke();
      if (c.motion === 'moving' && c.speed > 0.2) { const a = (c.course * Math.PI) / 180; ctx.beginPath(); ctx.moveTo(q.x, q.y); ctx.lineTo(q.x + Math.sin(a) * 14, q.y - Math.cos(a) * 14); ctx.stroke(); }
      ctx.fillStyle = col; ctx.font = '10px ui-monospace, Menlo, monospace';
      ctx.fillText(`#${c.id} last seen ${c.ageS.toFixed(0)} s ago · ${c.liveness === 'fresh' ? 'unlocated' : c.liveness}`, q.x, q.y + 14);
      ctx.globalAlpha = 1; continue;
    }
    const p = toPx(c.e, c.n), r = Math.max(4, c.radius * view.ppm), col = CLS[dominant(c.mix)];
    const focused = c.id === state.focusedId;
    ctx.globalAlpha = LIVENESS_ALPHA[c.liveness] ?? 0.7;
    ctx.fillStyle = col; ctx.globalAlpha *= 0.18; ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = LIVENESS_ALPHA[c.liveness] ?? 0.7;
    ctx.strokeStyle = focused ? '#ffd166' : col; ctx.lineWidth = focused ? 3 : 1.5;
    ctx.setLineDash(c.liveness === 'fresh' ? [] : c.liveness === 'unheard' ? [5, 3] : [2, 4]);
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.stroke();
    // ce ring: where it is with 95 % probability (the receiver's belief, PROTOCOL.md 5.3)
    {
      ctx.setLineDash([2, 3]); ctx.lineWidth = 1; ctx.strokeStyle = '#ff7eb6';
      ctx.beginPath(); ctx.arc(p.x, p.y, (c.radius + (c.ceShown ?? c.ce ?? 0)) * view.ppm, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
    }
    if (c.motion === 'moving' && c.speed > 0.2) {
      const a = (c.course * Math.PI) / 180, L = Math.max(r + 6, c.speed * 5 * view.ppm);
      ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(p.x + Math.sin(a) * L, p.y - Math.cos(a) * L); ctx.stroke();
    }
    ctx.fillStyle = '#fff'; ctx.font = 'bold 12px ui-monospace, Menlo, monospace'; ctx.fillText(String(c.count), p.x, p.y);
    ctx.fillStyle = focused ? '#ffd166' : col; ctx.font = '10px ui-monospace, Menlo, monospace';
    ctx.fillText(`#${c.id} ${mixLabel(c.mix)} ${c.motion}${c.liveness !== 'fresh' ? ` · ${c.liveness}` : ''}`, p.x, p.y + r + 8);
  }
  for (const c of m.rx?.contacts || []) {
    if (!c.child || c.departed) continue;
    const p = toPx(c.e, c.n); ctx.globalAlpha = LIVENESS_ALPHA[c.liveness] ?? 0.7; ctx.fillStyle = CLS[dominant(c.mix)];
    ctx.beginPath(); ctx.arc(p.x, p.y, 3, 0, Math.PI * 2); ctx.fill(); ctx.strokeStyle = '#ffd166'; ctx.lineWidth = 1; ctx.stroke();
  }
  ctx.globalAlpha = 1;
  // link banner
  if (m.wire && m.wire.up === false) { ctx.fillStyle = 'rgba(248,81,73,.85)'; ctx.fillRect(W / 2 - 60, 8, 120, 20); ctx.fillStyle = '#fff'; ctx.font = 'bold 12px monospace'; ctx.fillText('LINK BLACKOUT', W / 2, 18); }
}

// click -> focus + menu
topdown.addEventListener('click', (ev) => {
  const m = state.msg; if (!m) return;
  const rect = topdown.getBoundingClientRect(), x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  const { toPx } = topdownTransform(rect.width, rect.height);
  let best = null, bd = 1e9;
  for (const c of [...(m.rx?.contacts || []), ...(m.edge?.contacts || [])]) {
    const p = toPx(c.e, c.n), d = Math.hypot(p.x - x, p.y - y);
    if (d <= Math.max(14, c.radius * view.ppm) && d < bd) { bd = d; best = c; }
  }
  if (!best) { menu.hidden = true; return; }
  send({ cmd: 'focus', id: best.id, mode: 'auto' }); // a group splits, an individual of a split group drills
  state.menuId = best.id; $('menuId').textContent = `#${best.id}`;
  menu.style.left = `${Math.min(x + 8, rect.width - 230)}px`; menu.style.top = `${Math.min(y + 8, rect.height - 34)}px`; menu.hidden = false;
});
menu.addEventListener('click', (ev) => {
  const mode = ev.target?.dataset?.mode; if (!mode) return;
  send({ cmd: 'focus', id: state.menuId, mode });
  if (mode === 'release') menu.hidden = true;
});
document.addEventListener('click', (ev) => { if (!menu.contains(ev.target) && ev.target !== topdown) menu.hidden = true; });

// ---- controls ----------------------------------------------------------------------------------
playBtn.addEventListener('click', () => send({ cmd: state.playing ? 'pause' : 'play' }));
seek.addEventListener('pointerdown', () => { state.dragging = true; });
seek.addEventListener('input', () => { const now = performance.now(); if (now - state.lastSeekSent > 150) { state.lastSeekSent = now; send({ cmd: 'seek', t: Number(seek.value) }); } });
seek.addEventListener('change', () => { state.dragging = false; send({ cmd: 'seek', t: Number(seek.value) }); });
document.addEventListener('pointerup', () => { state.dragging = false; });
$('rates').addEventListener('click', (ev) => { const r = ev.target?.dataset?.rate; if (r) send({ cmd: 'rate', x: Number(r) }); });
$('profiles').addEventListener('click', (ev) => {
  const p = ev.target?.dataset?.profile; if (!p) return;
  if (p === 'blackout' && state.profile === 'blackout') send({ cmd: 'link', profile: state.lastProfile });
  else send({ cmd: 'link', profile: p });
});
document.addEventListener('keydown', (ev) => { if (ev.code === 'Space' && ev.target === document.body) { ev.preventDefault(); playBtn.click(); } });
video.addEventListener('play', () => { if (!state.playing) video.pause(); }); // the server owns play state

// ---- main --------------------------------------------------------------------------------------
function frame() { drawOverlay(); drawTopdown(); requestAnimationFrame(frame); }
loadCamera('/meta').then((cam) => { state.cam = cam; }).catch((e) => { hud.textContent = `meta: ${e.message}`; });
connect();
requestAnimationFrame(frame);
window.sbs = state; // debugging handle
