// Recon page (P6): the real TwinScene (entities, rings, frustum) from MinBand's WS :8080, with a
// voxel layer from hub/recon.py's SSE :8090 added via scene.addLayer(), a source-clip panel
// labelled "source clip (not transmitted)" (edge.py's own input footage -- the drone's actual
// frame data never leaves the edge), and a bottom bar reading the bridge's own /telemetry (B/s
// per class vs cap, ratio vs the H.264 baseline already in the snapshot, keyframe age, link state).
import * as THREE from 'three';
import { TwinScene } from './scene';
import { fmtBps, h264 } from './link';
import type { Snapshot } from './types';

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const setText = (id: string, text: string) => { const el = $(id); if (el.textContent !== text) el.textContent = text; };

const scene = new TwinScene($('scene'));
(import.meta.env.DEV) && Object.assign(window, { __minband_recon: { scene } });

const WS_URL = import.meta.env.VITE_WS_URL ?? `ws://${location.hostname}:8080`;
const RECON_URL = import.meta.env.VITE_RECON_URL ?? `http://${location.hostname}:8090`;
const TELEMETRY_URL = import.meta.env.VITE_TELEMETRY_URL ?? `http://${location.hostname}:28766`;

// ---- entities: same WS snapshot the main twin uses -------------------------------------------
let lastSnap: Snapshot | null = null;
function connectWs() {
  const ws = new WebSocket(WS_URL);
  ws.onopen = () => setText('statLink', linkState());
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type !== 'snapshot') return;
    lastSnap = m.snap as Snapshot;
    $('status').hidden = true;
    scene.update(lastSnap);
    const total = lastSnap.devices.reduce((a, d) => a + d.bps, 0);
    const v = h264(lastSnap, '720');
    setText('statH264', `${(total / Math.max(v.bps, 1) * 100).toFixed(1)}%`);
    setText('statLink', linkState());
  };
  ws.onclose = () => { setText('statLink', linkState()); setTimeout(connectWs, 1000); };
  return ws;
}
let ws = connectWs();

// ---- voxel layer: hub/recon.py's SSE deltas, additive per the wire format (hub/recon.py:SNAPSHOT
// resets its own hash per keyframe but only ever broadcasts newly-occupied voxels -- there is no
// "clear" event on the wire, so this layer accumulates for the session, same as the server does
// across a run). Capacity-capped InstancedMesh; one box per occupied 0.15 m voxel. ----------------
const VOXEL_M = 0.15, MAX_VOXELS = 20_000;
const voxelGeom = new THREE.BoxGeometry(VOXEL_M * 0.9, VOXEL_M * 0.9, VOXEL_M * 0.9);
const voxelMat = new THREE.MeshLambertMaterial({ color: 0x8ab4f8 });
const voxelMesh = new THREE.InstancedMesh(voxelGeom, voxelMat, MAX_VOXELS);
voxelMesh.count = 0;
scene.addLayer(voxelMesh);
const voxelIndex = new Map<string, number>();
const m4 = new THREE.Matrix4();

let lastKeyframes = 0, lastKeyframeAt: number | null = null, reconConnected = false;
function connectRecon() {
  const es = new EventSource(`${RECON_URL}/events`);
  es.onopen = () => { reconConnected = true; setText('statLink', linkState()); };
  es.onerror = () => { reconConnected = false; setText('statLink', linkState()); }; // EventSource auto-reconnects
  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.t === 'vox') {
      for (const [i, j, k] of msg.add as [number, number, number][]) {
        const key = `${i},${j},${k}`;
        if (voxelIndex.has(key) || voxelIndex.size >= MAX_VOXELS) continue;
        const idx = voxelIndex.size;
        voxelIndex.set(key, idx);
        m4.makeTranslation((i + 0.5) * VOXEL_M, (j + 0.5) * VOXEL_M, (k + 0.5) * VOXEL_M);
        voxelMesh.setMatrixAt(idx, m4);
      }
      voxelMesh.count = voxelIndex.size;
      voxelMesh.instanceMatrix.needsUpdate = true;
      setText('statVoxels', String(voxelIndex.size));
    } else if (msg.t === 'stats') {
      const kf = msg.keyframes as number;
      if (kf !== lastKeyframes) { lastKeyframes = kf; lastKeyframeAt = performance.now(); }
    }
  };
}
connectRecon();
setInterval(() => {
  setText('statKfAge', lastKeyframeAt === null ? 'no keyframe yet' : `${((performance.now() - lastKeyframeAt) / 1000).toFixed(1)} s`);
}, 500);

// ---- bridge telemetry: B/s per class vs cap (bridge.py's /telemetry, RX side) ------------------
let telemetryOk = false;
async function pollTelemetry() {
  try {
    const t = await (await fetch(`${TELEMETRY_URL}/telemetry`, { cache: 'no-store' })).json();
    telemetryOk = true;
    const sched = t.scheduler;
    if (sched) {
      const { data, telemetry, depth } = sched.bytes_per_sec;
      const total = data + telemetry + depth;
      setText('statTotal', fmtBps(total * 8));
      setText('statCap', fmtBps(sched.rate_preset_bytes_per_sec * 8));
      setText('statA', fmtBps(data * 8));
      setText('statB', fmtBps(telemetry * 8));
      setText('statC', fmtBps(depth * 8));
    }
  } catch {
    telemetryOk = false; // bridge not running -- bar just shows stale values, not an error
  }
  setText('statLink', linkState());
}
pollTelemetry();
setInterval(pollTelemetry, 2000);

function linkState(): string {
  const parts: string[] = [];
  if (ws.readyState !== WebSocket.OPEN) parts.push('no entities');
  if (!reconConnected) parts.push('no depth');
  if (!telemetryOk) parts.push('no telemetry');
  return parts.length ? parts.join(' · ') : 'live';
}

// ---- edge view: the same source clip edge.py is reading, looped -- this is what the drone's
// own camera sees, not a live feed (that frame data never leaves the edge).
function playSourceClip() {
  const v = $<HTMLVideoElement>('cam');
  v.src = '/clip/source.mp4';
  v.loop = true;
  v.play().catch(() => { $('edgePlaceholder').hidden = false; });
}
playSourceClip();
