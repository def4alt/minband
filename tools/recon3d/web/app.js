// MinBand 3D operator page. The reconstructed scene (MASt3R-SLAM map, moving objects masked out) with
// what the receiver decoded from the wire inside it: every contact at its dead-reckoned position and
// height, drawn as its own reconstructed surface points once its chip has arrived, as a class stand-in
// until then, with its honest error ring. ENU metres -> three.js: x = east, y = up, z = -north.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const INK = 0xe9ecef, INK2 = 0x9aa3ad, INK3 = 0x4a525b;
const CLASS = { 0: 'dismount', 1: 'bicycle', 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck' };
// Height of a chip's centre (the median of its visible surface) above the ground, by class: lift.py's H_C.
const H_C = { 0: 0.9, 1: 0.7, 2: 0.75, 3: 0.7, 5: 1.5, 7: 1.3 };
const v3 = (e, n, u) => new THREE.Vector3(e, u, -n);
const $ = (id) => document.getElementById(id);

// ---- renderer --------------------------------------------------------------------------------------
const host = $('scene');
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(2, devicePixelRatio));
renderer.setClearColor(0x070809);
host.appendChild(renderer.domElement);
const labels = new CSS2DRenderer();
labels.domElement.style.cssText = 'position:absolute;inset:0;pointer-events:none';
host.appendChild(labels.domElement);
const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0x070809, 160, 420);
scene.add(new THREE.HemisphereLight(0xffffff, 0x222222, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.2); sun.position.set(-40, 80, 30); scene.add(sun);
const camera = new THREE.PerspectiveCamera(55, 1, 0.3, 2000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
function resize() {
  const w = host.clientWidth, h = host.clientHeight;
  renderer.setSize(w, h); labels.setSize(w, h);
  camera.aspect = w / h; camera.updateProjectionMatrix();
}
addEventListener('resize', resize); resize();

// ---- the map and its terrain -----------------------------------------------------------------------
const CELL = 1.0;
let terrain = null; // { lo: [e, n], w, h, z: Float32Array (NaN = none) }
function groundAt(e, n) {
  if (!terrain) return null;
  const i = Math.floor((e - terrain.lo[0]) / CELL), j = Math.floor((n - terrain.lo[1]) / CELL);
  if (i < 0 || j < 0 || i >= terrain.w || j >= terrain.h) return null;
  const z = terrain.z[j * terrain.w + i];
  return Number.isFinite(z) ? z : null;
}
let mapPoints = null;
async function loadMap(name) {
  const q = `?s=${encodeURIComponent(name)}`;
  const [meta, buf] = await Promise.all([fetch(`/map.json${q}`).then((r) => r.json()), fetch(`/map.bin${q}`).then((r) => r.arrayBuffer())]);
  const n = meta.points, dv = new DataView(buf);
  const pos = new Float32Array(n * 3), col = new Uint8Array(n * 3);
  // Terrain for setting objects on the ground: the second-lowest point per 1 m cell.
  const lo = [meta.min[0], meta.min[1]], w = Math.ceil((meta.max[0] - lo[0]) / CELL) + 1, h = Math.ceil((meta.max[1] - lo[1]) / CELL) + 1;
  const a = new Float32Array(w * h).fill(Infinity), b = new Float32Array(w * h).fill(Infinity);
  for (let k = 0; k < n; k++) {
    const o = k * 16, e = dv.getFloat32(o, true), nn = dv.getFloat32(o + 4, true), u = dv.getFloat32(o + 8, true);
    pos[3 * k] = e; pos[3 * k + 1] = u; pos[3 * k + 2] = -nn;
    col[3 * k] = dv.getUint8(o + 12); col[3 * k + 1] = dv.getUint8(o + 13); col[3 * k + 2] = dv.getUint8(o + 14);
    const c = Math.floor((nn - lo[1]) / CELL) * w + Math.floor((e - lo[0]) / CELL);
    if (u < a[c]) { b[c] = a[c]; a[c] = u; } else if (u < b[c]) b[c] = u;
  }
  const z = new Float32Array(w * h);
  for (let c = 0; c < w * h; c++) z[c] = Number.isFinite(b[c]) ? b[c] : NaN;
  terrain = { lo, w, h, z };
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('color', new THREE.BufferAttribute(col, 3, true));
  const m = new THREE.PointsMaterial({ size: Math.max(0.12, meta.voxel_m * 1.3), vertexColors: true, sizeAttenuation: true });
  if (mapPoints) { scene.remove(mapPoints); mapPoints.geometry.dispose(); }
  mapPoints = new THREE.Points(g, m);
  scene.add(mapPoints);
  $('vMap').textContent = `${(n / 1000).toFixed(0)}k pts · ${meta.voxel_m} m`;
}

// ---- chips: each object as a solid of its measured size, wearing the drone's view of it --------------
// A chip (lift.py) carries the object's footprint from the reconstruction, the masked crop the drone saw,
// and the projection from the object's frame (x forward, y left, z up, metres) into that crop. The
// shader projects the crop onto the faces the drone saw; elsewhere the solid is matte ink.
let chipData = {};
const chipTex = new Map();
const chipVert = `
  varying vec3 vObj; varying vec3 vNObj; varying vec3 vNView;
  void main() {
    // three local (x = -left, y = up, z = -forward) -> object frame (forward, left, up)
    vObj = vec3(-position.z, -position.x, position.y);
    vNObj = vec3(-normal.z, -normal.x, normal.y);
    vNView = normalize(normalMatrix * normal);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }`;
const chipFrag = `
  uniform sampler2D uTex; uniform mat4 uProj; uniform vec3 uCam; uniform float uOpacity; uniform vec3 uInk;
  varying vec3 vObj; varying vec3 vNObj; varying vec3 vNView;
  void main() {
    float shade = 0.62 + 0.38 * max(dot(vNView, normalize(vec3(-0.3, 0.8, 0.5))), 0.0);
    vec3 col = uInk * shade;
    vec4 h = uProj * vec4(vObj, 1.0);
    vec2 uv = h.xy / h.z;
    bool seen = h.z > 0.0 && dot(vNObj, uCam - vObj) > 0.0 && uv.x > 0.0 && uv.x < 1.0 && uv.y > 0.0 && uv.y < 1.0;
    if (seen) {
      vec4 t = texture2D(uTex, vec2(uv.x, 1.0 - uv.y));
      col = mix(col, t.rgb * (0.85 + 0.15 * shade), smoothstep(0.35, 0.65, t.a));
    }
    gl_FragColor = vec4(col, uOpacity);
  }`;
function chipMaterial(id) {
  const c = chipData[id];
  if (!c) return null;
  let tex = chipTex.get(id);
  if (!tex) {
    const img = new Image();
    img.src = `data:image/webp;base64,${c.webp}`;
    tex = new THREE.Texture(img);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.minFilter = THREE.LinearFilter;
    img.onload = () => { tex.needsUpdate = true; };
    chipTex.set(id, tex);
  }
  const P = c.proj;  // 3x4 row-major -> mat4 rows 0..2, last row unused
  const m = new THREE.Matrix4().set(P[0], P[1], P[2], P[3], P[4], P[5], P[6], P[7], P[8], P[9], P[10], P[11], 0, 0, 0, 1);
  return new THREE.ShaderMaterial({
    uniforms: { uTex: { value: tex }, uProj: { value: m }, uCam: { value: new THREE.Vector3(...c.cam) }, uOpacity: { value: 1 }, uInk: { value: new THREE.Color(0x8d949c) } },
    vertexShader: chipVert, fragmentShader: chipFrag, transparent: true,
  });
}
function chipMesh(id) {
  const c = chipData[id];
  const mat = c && chipMaterial(id);
  if (!mat) return null;
  const [len, wid, hgt] = c.size;
  // The solid in three local coordinates: width along x, height along y, length along z; standing on
  // the ground, which is `base` below the object frame's origin.
  const g = new THREE.BoxGeometry(wid, hgt, len, 2, 2, 4);
  g.translate(0, c.base + hgt / 2, 0);
  return new THREE.Mesh(g, mat);
}

// ---- contacts --------------------------------------------------------------------------------------
const standIn = {
  car: new THREE.BoxGeometry(1.8, 1.5, 4.4), truck: new THREE.BoxGeometry(2.4, 2.6, 7.0), bus: new THREE.BoxGeometry(2.5, 3.0, 11.0),
  cycle: new THREE.BoxGeometry(0.7, 1.2, 1.9), person: new THREE.CapsuleGeometry(0.28, 1.1, 4, 8),
};
const solid = new THREE.MeshStandardMaterial({ color: INK, roughness: 0.9, metalness: 0, transparent: true, opacity: 0.9 });
const ringMat = new THREE.LineBasicMaterial({ color: INK2, transparent: true, opacity: 0.55 });
const ringLost = new THREE.LineDashedMaterial({ color: INK2, dashSize: 0.8, gapSize: 0.6, transparent: true, opacity: 0.5 });
const unitCircle = (() => { const p = []; for (let k = 0; k <= 64; k++) { const a = (k / 64) * Math.PI * 2; p.push(new THREE.Vector3(Math.cos(a), 0, Math.sin(a))); } return new THREE.BufferGeometry().setFromPoints(p); })();
const objects = new Map();
let showRings = true, showTruth = false;

function shapeFor(c) {
  if (c.group) return null;
  const cls = c.chip ? chipData[c.chip]?.cls : null;
  const coarse = c.mix[0] ? 'person' : null;
  const name = cls === 7 ? 'truck' : cls === 5 ? 'bus' : cls === 1 || cls === 3 ? 'cycle' : cls === 0 || coarse ? 'person' : 'car';
  return name;
}

function contactObject(c) {
  let o = objects.get(c.id);
  if (!o) {
    const root = new THREE.Group();
    const ring = new THREE.LineLoop(unitCircle, ringMat);
    scene.add(ring);
    const el = document.createElement('div'); el.className = 'tag';
    const tag = new CSS2DObject(el); root.add(tag);
    scene.add(root);
    o = { root, ring, tag, el, body: null, bodyKey: '', trail: [], trailLine: null };
    objects.set(c.id, o);
  }
  // The body: the chip once it arrived, else the class stand-in; a group is a ring and a count.
  const key = c.group ? 'group' : c.chip ? `chip:${c.chip}` : `stand:${shapeFor(c)}`;
  if (key !== o.bodyKey) {
    if (o.body) o.root.remove(o.body);
    o.body = null;
    if (c.chip && chipData[c.chip]) {
      o.body = chipMesh(c.chip);
    } else if (!c.group) {
      const s = shapeFor(c);
      // Centred on the root, which sits at the class's centre height above the ground: the bottom touches it.
      o.body = new THREE.Mesh(standIn[s], solid.clone());
    }
    if (o.body) o.root.add(o.body);
    o.bodyKey = key;
  }
  return o;
}

function heightFor(c) {
  const cls = c.chip ? chipData[c.chip]?.cls ?? 2 : c.mix[0] ? 0 : 2;
  const g = groundAt(c.e, c.n);
  const onGround = g != null ? g + (H_C[cls] ?? 0.75) : null;
  // dz says which surface (metre steps); the map says exactly where that surface is.
  if (onGround != null && (c.u == null || Math.abs(c.u - onGround) < 2.5)) return { u: onGround, ground: g };
  return { u: c.u ?? (H_C[cls] ?? 0.75), ground: (c.u ?? 0.75) - (H_C[cls] ?? 0.75) };
}

const trailMat = new THREE.LineBasicMaterial({ color: INK3, transparent: true, opacity: 0.7 });
function updateContacts(list, t) {
  const live = new Set();
  for (const c of list) {
    live.add(c.id);
    const o = contactObject(c);
    const { u, ground } = heightFor(c);
    o.root.position.copy(v3(c.e, c.n, u));
    const chip = c.chip ? chipData[c.chip] : null;
    const yaw = c.motion === 'moving' && c.speed > 1.5 ? c.course : chip ? chip.yaw0 : c.course || 0;
    o.root.rotation.y = -yaw * Math.PI / 180;
    const fresh = c.liveness === 'fresh';
    if (o.body) {
      const op = fresh ? (c.chip ? 1 : 0.85) : c.liveness === 'unheard' ? 0.55 : 0.3;
      if (o.body.material.uniforms) o.body.material.uniforms.uOpacity.value = op; else o.body.material.opacity = op;
    }
    // The error ring: what the receiver can vouch for (ce_shown), on the ground.
    const r = Math.max(0.6, c.group ? Math.max(c.radius, c.ceShown) : c.ceShown);
    o.ring.visible = showRings;
    o.ring.position.copy(v3(c.e, c.n, ground + 0.08));
    o.ring.scale.set(r, 1, r);
    o.ring.material = c.lost || !fresh ? ringLost : ringMat;
    if (o.ring.material === ringLost) o.ring.computeLineDistances();
    const name = c.group ? `${c.count} ${c.mix[0] >= c.mix[1] ? 'dismounts' : 'vehicles'}` : chip ? CLASS[chip.cls] : c.mix[0] ? 'dismount' : 'vehicle';
    o.el.textContent = `${name}${c.motion === 'moving' ? ` ${c.speed.toFixed(1)} m/s` : ''}${c.chip ? '' : c.group ? '' : ' · chip pending'}`;
    o.el.className = fresh ? 'tag' : 'tag dim';
    o.tag.position.set(0, 1.6, 0);
    // Labels only on hover (docs/STYLE.md), and on groups, which have no body to read.
    o.tag.visible = c.group || hovered === c.id;
    // A 3 s trail of where the twin had it.
    o.trail.push({ t, p: v3(c.e, c.n, ground + 0.1) });
    while (o.trail.length && t - o.trail[0].t > 3) o.trail.shift();
    if (o.trailLine) { scene.remove(o.trailLine); o.trailLine.geometry.dispose(); }
    o.trailLine = o.trail.length > 1 ? new THREE.Line(new THREE.BufferGeometry().setFromPoints(o.trail.map((x) => x.p)), trailMat) : null;
    if (o.trailLine) scene.add(o.trailLine);
  }
  for (const [id, o] of objects) if (!live.has(id)) {
    scene.remove(o.root); scene.remove(o.ring); if (o.trailLine) scene.remove(o.trailLine);
    o.el.remove(); objects.delete(id);
  }
}

// ---- the drone ---------------------------------------------------------------------------------------
const frustum = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: 0.9 }));
const nadir = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineDashedMaterial({ color: INK3, dashSize: 0.6, gapSize: 0.5 }));
scene.add(frustum, nadir);
let hfov = 67;
function camBasis(p) {
  const yaw = p.yaw * Math.PI / 180, pitch = p.pitch * Math.PI / 180, roll = (p.roll || 0) * Math.PI / 180;
  const fwd = v3(Math.sin(yaw) * Math.cos(pitch), Math.cos(yaw) * Math.cos(pitch), Math.sin(pitch));
  let right = v3(Math.cos(yaw), -Math.sin(yaw), 0);
  let up = new THREE.Vector3().crossVectors(right, fwd);
  if (roll) { right.applyAxisAngle(fwd, -roll); up.applyAxisAngle(fwd, -roll); }
  return { fwd, right, up };
}
function updateDrone(p) {
  if (!p) { frustum.visible = nadir.visible = false; return; }
  frustum.visible = nadir.visible = true;
  const c = v3(p.e, p.n, p.u), { fwd, right, up } = camBasis(p);
  const d = 10, hw = d * Math.tan((hfov / 2) * Math.PI / 180), hh = hw * 9 / 16;
  const corners = [[1, 1], [-1, 1], [-1, -1], [1, -1]].map(([x, y]) => c.clone().addScaledVector(fwd, d).addScaledVector(right, x * hw).addScaledVector(up, y * hh));
  const seg = [];
  for (const k of corners) seg.push(c, k);
  for (let k = 0; k < 4; k++) seg.push(corners[k], corners[(k + 1) % 4]);
  frustum.geometry.setFromPoints(seg);
  const g = groundAt(p.e, p.n);
  nadir.geometry.setFromPoints([c, v3(p.e, p.n, g ?? 0)]); nadir.computeLineDistances();
}

// ---- edge truth (what the edge tracked; not what crossed the link) ----------------------------------
const truthMat = new THREE.PointsMaterial({ color: INK3, size: 0.5, sizeAttenuation: true });
const truth = new THREE.Points(new THREE.BufferGeometry(), truthMat);
scene.add(truth);
function updateTruth(tracks) {
  truth.visible = showTruth;
  if (!showTruth) return;
  const a = new Float32Array(tracks.length * 3);
  tracks.forEach((x, k) => { a[3 * k] = x.e; a[3 * k + 1] = x.u + 1.4; a[3 * k + 2] = -x.n; });
  truth.geometry.setAttribute('position', new THREE.BufferAttribute(a, 3));
}

// ---- views -----------------------------------------------------------------------------------------
let view = 'chase', lastPose = null;
const VIEWS = ['chase', 'drone', 'orbit', 'top'];
$('views').innerHTML = VIEWS.map((v) => `<button data-v="${v}">${v}</button>`).join('');
$('views').onclick = (ev) => { const v = ev.target.dataset?.v; if (v) { view = v; placeView(true); markButtons(); } };
function placeView(jump) {
  const p = lastPose;
  if (!p) return;
  const c = v3(p.e, p.n, p.u), { fwd, up } = camBasis(p);
  const look = c.clone().addScaledVector(fwd, 60);
  if (view === 'drone') { camera.position.copy(c); camera.up.copy(up); camera.lookAt(look); controls.enabled = false; return; }
  camera.up.set(0, 1, 0); controls.enabled = true;
  if (!jump) return;
  if (view === 'chase') {
    // Above and behind the drone, looking at where its camera looks on the ground.
    const dep = Math.max(8, -p.pitch) * Math.PI / 180, g = groundAt(p.e, p.n) ?? 0;
    const reach = Math.min(150, (p.u - g) / Math.tan(dep));
    const flat = fwd.clone().setY(0).normalize();
    const aim = v3(p.e, p.n, g).addScaledVector(flat, reach * 0.8);
    camera.position.copy(c).addScaledVector(flat, -30).add(new THREE.Vector3(0, 32, 0)); controls.target.copy(aim);
  }
  if (view === 'orbit') { camera.position.copy(look).add(new THREE.Vector3(70, 60, 70)); controls.target.copy(look); }
  if (view === 'top') { camera.position.copy(look).add(new THREE.Vector3(0, 140, 0.01)); controls.target.copy(look); }
  controls.update();
}

// ---- hover -----------------------------------------------------------------------------------------
let hovered = null;
const ray = new THREE.Raycaster(); ray.params.Points.threshold = 0.4;
renderer.domElement.addEventListener('pointermove', (ev) => {
  const r = renderer.domElement.getBoundingClientRect();
  ray.setFromCamera(new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1), camera);
  hovered = null;
  let best = Infinity;
  for (const [id, o] of objects) {
    if (!o.body) continue;
    const hit = ray.intersectObject(o.body, false)[0];
    if (hit && hit.distance < best) { best = hit.distance; hovered = id; }
  }
});

// ---- video and the edge's boxes ------------------------------------------------------------------
const video = $('video'), overlay = $('overlay'), octx = overlay.getContext('2d');
function drawBoxes(tracks) {
  const w = overlay.clientWidth, h = overlay.clientHeight;
  if (overlay.width !== w) overlay.width = w;
  if (overlay.height !== h) overlay.height = h;
  octx.clearRect(0, 0, w, h);
  octx.strokeStyle = 'rgba(233,236,239,.85)'; octx.lineWidth = 1;
  for (const x of tracks) {
    const [bu, bv, bw, bh] = x.bbox;
    octx.strokeRect((bu - bw / 2) * w, (bv - bh / 2) * h, bw * w, bh * h);
  }
}

// ---- the link ----------------------------------------------------------------------------------------
const PROFILES = ['clean', 'hf', 'lora', 'telemetry', 'blackout'];
$('profiles').innerHTML = PROFILES.map((p) => `<button data-p="${p}">${p}</button>`).join('');
const ws = new WebSocket(`ws://${location.host}`);
const send = (m) => ws.readyState === 1 && ws.send(JSON.stringify(m));
$('profiles').onclick = (ev) => { const p = ev.target.dataset?.p; if (p) send({ cmd: 'link', profile: p }); };
let state = null;
$('bPlay').onclick = () => send({ cmd: state?.playing ? 'pause' : 'play' });
$('scenes').onclick = (ev) => { const n = ev.target.dataset?.s; if (n) send({ cmd: 'scene', name: n }); };
$('bStart').onclick = () => send({ cmd: 'seek', t: 0 });
$('bTruth').onclick = () => { showTruth = !showTruth; markButtons(); };
$('bRings').onclick = () => { showRings = !showRings; markButtons(); };
function markButtons() {
  for (const b of $('profiles').children) b.classList.toggle('on', b.dataset.p === state?.profile);
  for (const b of $('views').children) b.classList.toggle('on', b.dataset.v === view);
  for (const b of $('scenes').children) b.classList.toggle('on', b.dataset.s === state?.scene);
  $('bTruth').classList.toggle('on', showTruth); $('bRings').classList.toggle('on', showRings);
  $('bPlay').textContent = state?.playing === false ? 'Play' : 'Pause';
}

const fmtB = (b) => (b >= 1000 ? `${(b / 1000).toFixed(1)} kB` : `${b} B`);
let firstState = true;
ws.onmessage = (ev) => {
  const prev = state;
  state = JSON.parse(ev.data);
  const s = state;
  lastPose = s.rx.pose && s.video ? s.rx.pose : s.rx.ego ? { e: s.rx.ego.e, n: s.rx.ego.n, u: s.rx.ego.altAgl, yaw: s.rx.ego.heading, pitch: lastPose?.pitch ?? -15, roll: 0 } : lastPose;
  updateDrone(lastPose);
  if (firstState && lastPose) { placeView(true); firstState = false; }
  if (view === 'drone') placeView(false);
  if (s.scene !== sceneName) { loadScene(s.scene); $('scenes').innerHTML = s.scenes.map((n) => `<button data-s="${n}">${n}</button>`).join(''); markButtons(); }
  updateContacts(s.rx.contacts, s.t);
  updateTruth(s.edge.tracks);
  // The video plays along and is re-seeked only on real drift: a seek every step never completes.
  if (s.video) {
    if (s.playing && video.paused) video.play().catch(() => {});
    if (!s.playing && !video.paused) video.pause();
    if (Math.abs(video.currentTime - s.t) > 0.4 && !video.seeking) video.currentTime = Math.min(s.t, s.footageS);
    drawBoxes(s.edge.tracks);
  } else if (!video.paused) video.pause();
  $('videoBox').classList.toggle('down', !s.video);
  const budget = s.budgetBps ? `${(s.budgetBps / 1000).toFixed(1)} kbit/s` : 'video link';
  $('vWire').textContent = `${fmtB(s.wire.bytesPerS)}/s · ${budget}`;
  $('vTwin').textContent = `${s.rx.known}${s.rx.of >= 0 ? ` of ${s.rx.of}` : ''} known · lvl ${s.edge.level}`;
  $('vChips').textContent = `${s.wire.chipsDelivered} in · ${s.wire.chipQueue} queued · ${fmtB(s.wire.chipBytes)}`;
  $('vTime').textContent = `${s.t.toFixed(1)} / ${s.footageS.toFixed(1)} s`;
  $('events').innerHTML = s.rx.events.slice().reverse().map((e) => `<div><span>${e.t.toFixed(1)}</span>${e.text}</div>`).join('');
  if (!prev || prev.profile !== s.profile || prev.playing !== s.playing) markButtons();
};

$('note').innerHTML = 'Contacts, heights and the drone pose cross the link as MinBand frames (real WASM edge, shaped link, WASM receiver). '
  + 'The map is 3d-map-stream\'s channel, loaded whole here. Chips travel a simulated channel at half the link with their real compressed size; '
  + 'the core does not put ChipSym records on the wire yet.';

function frame() {
  controls.update();
  renderer.render(scene, camera);
  labels.render(scene, camera);
  requestAnimationFrame(frame);
}
// For scripts/screenshot.mjs --eval: look at things from code.
window.minband = { THREE, camera, controls, objects, v3, get state() { return state; }, setView: (v) => { view = v; markButtons(); } };
// A scene switch (or the first state) loads that scene's map, chips and video.
let sceneName = null;
function loadScene(name) {
  sceneName = name;
  for (const [, o] of objects) { scene.remove(o.root); scene.remove(o.ring); if (o.trailLine) scene.remove(o.trailLine); o.el.remove(); }
  objects.clear(); chipTex.clear(); chipData = {};
  const q = `?s=${encodeURIComponent(name)}`;
  fetch(`/summary.json${q}`).then((r) => r.json()).then((s) => { hfov = s.hfov_deg; });
  fetch(`/chips.json${q}`).then((r) => r.json()).then((c) => { if (sceneName === name) chipData = c; });
  video.src = `/video.mp4${q}`;
  loadMap(name);
  firstState = true;
}
markButtons();
frame();
