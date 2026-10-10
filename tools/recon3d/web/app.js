// MinBand 3D operator page: a SIMULATION of what the operator sees. The reconstruction map is not sent
// (it stays on the drone, which used it only to place the detected objects in 3D); here a synthetic world,
// a ground raised to the contacts' own base heights (below), holds what the receiver decoded from the wire:
// every contact at its dead-reckoned position and the height that crossed the link, with its orientation and
// its honest error ring, seen from the drone's received pose. ENU metres -> three.js: x = east, y = up, z = -north.
//
// The driver steps at 10 Hz; the page draws between the previous and the current state (100 ms behind),
// so motion is smooth at any frame rate. ?rec=1 hides the video and the side panel (scripts/record.mjs),
// ?view=drone starts in the drone's camera, ?ground=flat the old flat u = 0 ground, ?snap=0 no settling on it.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const INK = 0xe9ecef, INK2 = 0x9aa3ad, INK3 = 0x4a525b;
const CLASS = { 0: 'dismount', 1: 'bicycle', 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck' };
// Height of a chip's centre (the median of its visible surface) above the ground, by class: lift.py's H_C.
const H_C = { 0: 0.9, 1: 0.7, 2: 0.75, 3: 0.7, 5: 1.5, 7: 1.3 };
const v3 = (e, n, u) => new THREE.Vector3(e, u, -n);
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const REC = params.get('rec') === '1';
const STEP_MS = 100;                                 // the driver's step; the interpolation spans one
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const lerp = (a, b, t) => a + (b - a) * t;
const wrap180 = (d) => ((((d + 180) % 360) + 360) % 360) - 180;
const lerpAng = (a, b, t) => a + wrap180(b - a) * t;   // the short way round: 359 -> 1 degree is +2, not -358
if (REC) document.body.classList.add('rec');

// ---- renderer --------------------------------------------------------------------------------------
// Sky and fog share the horizon colour, so the ground fades into the sky with no visible edge.
const HORIZON = 0x14181c, ZENITH = 0x060708, GROUND = 0x0e1114;
const host = $('scene');
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: REC });
renderer.setPixelRatio(REC ? 1 : Math.min(2, devicePixelRatio));
renderer.setClearColor(HORIZON);
host.appendChild(renderer.domElement);
const labels = new CSS2DRenderer();
labels.domElement.style.cssText = 'position:absolute;inset:0;pointer-events:none';
host.appendChild(labels.domElement);
const scene = new THREE.Scene();
scene.fog = new THREE.Fog(HORIZON, 140, 420);
scene.add(new THREE.HemisphereLight(0xffffff, 0x222222, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.2); sun.position.set(-40, 80, 30); scene.add(sun);
const camera = new THREE.PerspectiveCamera(55, 1, 0.5, 2000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
let hfov = 67, imgAspect = 16 / 9;                   // the footage's camera (summary.json), set when the scene loads
// The drone's camera: the vertical fov that gives the footage's horizontal fov on this canvas. Elsewhere 55.
function fovFor(v) { return v === 'drone' ? 2 * Math.atan(Math.tan((hfov / 2) * Math.PI / 180) / camera.aspect) * 180 / Math.PI : 55; }
function setFov() { const f = fovFor(view); if (Math.abs(camera.fov - f) > 1e-6) { camera.fov = f; camera.updateProjectionMatrix(); } }
function resize() {
  const w = host.clientWidth, h = host.clientHeight;
  renderer.setSize(w, h); labels.setSize(w, h);
  camera.aspect = w / h; camera.updateProjectionMatrix(); setFov();
}
addEventListener('resize', () => resize());

// ---- the simulated world: sky and a ground built from what the contacts say ----------------------------
// The u = 0 plane is the drone's RANSAC fit, not the local ground (on the Labrador clip the objects stand 5-9 m
// above it up to 500 m out, ~19 m beyond), so the operator's ground is a coarse height field made only of what the
// receiver decoded: every contact with a height gives a base (u - the class's centre height) at its position.
//   per 16 m cell a low percentile of the bases (a car on a roof or a pole read as a person sits above the ground,
//   never below it), cells filled by a Gaussian-kernel average (30 m) of the bases' residuals from a robust plane fit of them
//   (not 0), so the field is that plane beyond ~100 m from any contact, the target rebuilt twice a second, the shown field eased towards it
//   so the ground never jumps. Samples outlive their contact (a lost or departed contact stood on real ground), so
//   the field keeps what the drone has flown over. ?ground=flat is the old u = 0 plane; ?snap=0 draws every object
//   at the height it was sent instead of settling it on the field.
const FLAT = params.get('ground') === 'flat', SNAP = params.get('snap') !== '0';
const GCELL = 16, GN = 128;                         // 16 m cells, a 2 km window (the rest is clamped; the fog hides it)
const GQ = 0.35, GCAP = 3, GMEM_S = 120;            // cell percentile, weight cap per cell, how long a sample outlives its contact
const G_SIGMA = 30, G_REACH = 100, G_PRIOR = 0.25;  // the kernel's scale, where a contact stops counting (3.3 sigma), the plane's weight
const G_SLOPE = 0.12, G_RIDGE = 900, G_RES = 2;     // steepest plane, its slope ridge (resid sd^2 / slope sd^2), robust residual scale (m)
const G_REBUILD_S = 0.5, G_TAU_S = 2;               // rebuild period, easing time constant (display time: frame-exact in recordings)
const SNAP_M = 3;                                   // an object within 3 m of the ground stands on it (base noise is ~2 m); beyond 6 m it keeps its sent height
$('vGround').textContent = FLAT ? 'flat sim · objects at sent height' : 'simulated · from contact bases';
const G = {
  x0: 0, z0: 0, set: false, samples: new Map(),     // window corner (world x, z of cell 0's centre), contact id -> sample
  target: new Float32Array(GN * GN), shown: new Float32Array(GN * GN), tBuilt: -1e9, tShown: null, jump: true, tex: null,
};
G.tex = new THREE.DataTexture(G.shown, GN, GN, THREE.RedFormat, THREE.FloatType);
G.tex.minFilter = G.tex.magFilter = THREE.NearestFilter; G.tex.needsUpdate = true;   // bilinear in the shader: float textures need no filter extension

// Cells -> representatives {x, z, h, w}: the weighted GQ-percentile of each cell's bases, at the weighted mean position.
function groundPoints(excl) {
  const cells = new Map();
  for (const [id, s] of G.samples) if (id !== excl) { const k = s.i * GN + s.j; (cells.get(k) ?? cells.set(k, []).get(k)).push(s); }
  const pts = [];
  for (const list of cells.values()) {
    list.sort((p, q) => p.h - q.h);
    const W = list.reduce((a, s) => a + s.w, 0);
    let acc = 0, h = list[list.length - 1].h, x = 0, z = 0;
    for (const s of list) { x += s.w * s.x; z += s.w * s.z; }
    for (const s of list) { acc += s.w; if (acc >= GQ * W) { h = s.h; break; } }
    pts.push({ x: x / W, z: z / W, h, w: Math.min(W, GCAP) });
  }
  return pts;
}
// A plane h = a + b x + c z through the representatives: weighted least squares with a ridge on the slopes (collinear
// contacts), two reweighting passes that discount outliers (a roof, a hill the contacts do not span), slope clamped.
function groundPlane(pts) {
  if (!pts.length) return { a: 0, b: 0, c: 0 };
  const rw = pts.map(() => 1);
  let pl = { a: 0, b: 0, c: 0 };
  for (let it = 0; it < 3; it++) {
    let W = 0, mx = 0, mz = 0, mh = 0;
    pts.forEach((p, k) => { const w = p.w * rw[k]; W += w; mx += w * p.x; mz += w * p.z; mh += w * p.h; });
    mx /= W; mz /= W; mh /= W;
    let sxx = G_RIDGE, sxz = 0, szz = G_RIDGE, sxh = 0, szh = 0;
    pts.forEach((p, k) => { const w = p.w * rw[k], dx = p.x - mx, dz = p.z - mz, dh = p.h - mh; sxx += w * dx * dx; sxz += w * dx * dz; szz += w * dz * dz; sxh += w * dx * dh; szh += w * dz * dh; });
    const det = sxx * szz - sxz * sxz;
    let b = (szz * sxh - sxz * szh) / det, c = (sxx * szh - sxz * sxh) / det;
    const g = Math.hypot(b, c); if (g > G_SLOPE) { b *= G_SLOPE / g; c *= G_SLOPE / g; }
    pl = { a: mh - b * mx - c * mz, b, c };
    pts.forEach((p, k) => { const r = p.h - (pl.a + pl.b * p.x + pl.c * p.z); rw[k] = 1 / (1 + (r / G_RES) ** 2); });
  }
  return pl;
}
// The field at (x, z): the plane plus a Gaussian-kernel average of the representatives' residuals from it (so a slope is
// not flattened), the plane alone where no representative is within reach (a weight G_PRIOR competes with their summed
// weights, so the correction fades out smoothly as the data thins). Smooth at the kernel's scale, far finer than any
// one contact's height can claim: a contact's own base is noisy by a metre or two, the average of those near it is not.
function groundIdw(pts, pl, x, z) {
  let num = 0, den = G_PRIOR;
  for (const p of pts) {
    const dx = p.x - x, dz = p.z - z;
    if (dx > G_REACH || dx < -G_REACH || dz > G_REACH || dz < -G_REACH) continue;
    const w = p.w * Math.exp(-(dx * dx + dz * dz) / (2 * G_SIGMA * G_SIGMA));
    num += w * (p.h - (pl.a + pl.b * p.x + pl.c * p.z)); den += w;
  }
  return pl.a + pl.b * x + pl.c * z + num / den;
}
function rebuildGround(t, snap) {
  G.tBuilt = t;
  for (const [id, s] of G.samples) if (t - s.t > GMEM_S) G.samples.delete(id);
  const pts = groundPoints(), pl = groundPlane(pts);
  const T = G.target;
  for (let j = 0; j < GN; j++) for (let i = 0; i < GN; i++) T[j * GN + i] = pl.a + pl.b * (G.x0 + i * GCELL) + pl.c * (G.z0 + j * GCELL);
  // The cells within reach of a representative get the kernel average; the rest keep the plane.
  if (pts.length) {
    const lo = (v) => clamp(Math.floor((v - G_REACH - G.x0) / GCELL), 0, GN - 1), hi = (v) => clamp(Math.ceil((v + G_REACH - G.x0) / GCELL), 0, GN - 1);
    const i0 = lo(Math.min(...pts.map((p) => p.x))), i1 = hi(Math.max(...pts.map((p) => p.x)));
    const zlo = (v) => clamp(Math.floor((v - G_REACH - G.z0) / GCELL), 0, GN - 1), zhi = (v) => clamp(Math.ceil((v + G_REACH - G.z0) / GCELL), 0, GN - 1);
    const j0 = zlo(Math.min(...pts.map((p) => p.z))), j1 = zhi(Math.max(...pts.map((p) => p.z)));
    for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) T[j * GN + i] = groundIdw(pts, pl, G.x0 + i * GCELL, G.z0 + j * GCELL);
  }
  if (snap || G.jump) { G.shown.set(T); G.tex.needsUpdate = true; G.jump = false; }
}
// Ease the shown field towards the target by the display time elapsed (so a recording at any frame rate is the same).
function easeGround(tt, snap) {
  const dt = G.tShown == null || snap ? 1e9 : tt - G.tShown;
  G.tShown = tt;
  if (dt <= 0) return;
  const k = 1 - Math.exp(-dt / G_TAU_S), S = G.shown, T = G.target;
  let moved = 0;
  for (let q = 0; q < S.length; q++) { const d = T[q] - S[q]; if (d !== 0) { S[q] += d * k; if (Math.abs(d) > moved) moved = Math.abs(d); } }
  if (moved > 1e-3) G.tex.needsUpdate = true;
}
// The shown ground height at world (x, z): the same bilinear the vertex shader does. 0 for ?ground=flat.
function groundAt(x, z) {
  if (FLAT) return 0;
  const fx = clamp((x - G.x0) / GCELL, 0, GN - 1.001), fz = clamp((z - G.z0) / GCELL, 0, GN - 1.001);
  const i = Math.floor(fx), j = Math.floor(fz), tx = fx - i, tz = fz - j, S = G.shown, q = j * GN + i;
  return lerp(lerp(S[q], S[q + 1], tx), lerp(S[q + GN], S[q + GN + 1], tx), tz);
}
// An object's base is the ground under it where they agree within SNAP_M (the field is the consensus; one contact's
// height is noisy by a metre or two), and its sent height beyond 2 SNAP_M (a deck, a roof, a hill the cells smooth
// over) up to SNAP_MAX_M; above that no road user stands on the ground's far side (it is a height error, usually of a
// contact whose ring is metres wide), and it is drawn on the ground again rather than hovering. Not in the drone view:
// there the sent height is what puts the object on its pixels (a 3 m drop moves a car 200 m out by ~20 px at a 15 deg
// look-down), so every object stays where it crossed the link and the ground comes up to it.
const SNAP_MAX_M = 12;
function standOn(d) {
  if (!SNAP || FLAT || view === 'drone') return d;
  const a = Math.abs(d), up = clamp((a - SNAP_M) / SNAP_M, 0, 1), down = 1 - clamp((a - SNAP_MAX_M) / SNAP_M, 0, 1);
  return d * (up * up * (3 - 2 * up)) * (down * down * (3 - 2 * down));
}

const colorOut = '#include <tonemapping_fragment>\n#include <colorspace_fragment>';
// The ground is a shader: a matte surface, its vertices raised to the height field (a texture read in the vertex
// shader, bilinear between cells), with an analytic grid (major lines every 10 m, faint minor every 2 m) from world
// xz, each line one pixel wide and faded out as its cells approach the pixel size, so it neither shimmers nor
// aliases at the drone's oblique 15-40 degree look-down. It follows the camera, is finer near it than far, and
// fades into the fog well before its 1.5 km edge.
const GROUND_R = 1500, GROUND_SEGS = 256;
const groundGeo = new THREE.PlaneGeometry(2, 2, GROUND_SEGS, GROUND_SEGS).rotateX(-Math.PI / 2);
{ const p = groundGeo.attributes.position; for (let k = 0; k < p.count; k++) { const w = (s) => GROUND_R * (0.15 * s + 0.85 * s * s * s); p.setXYZ(k, w(p.getX(k)), 0, w(p.getZ(k))); } }
const ground = new THREE.Mesh(groundGeo,
  new THREE.ShaderMaterial({
    polygonOffset: true, polygonOffsetFactor: 2, polygonOffsetUnits: 2,   // rings, trails and feet sit a few cm above it, far or near
    uniforms: {
      uGround: { value: new THREE.Color(GROUND) }, uMinor: { value: new THREE.Color(0x1a1e23) }, uMajor: { value: new THREE.Color(0x343a41) },
      uFog: { value: new THREE.Color(HORIZON) }, uFogRange: { value: new THREE.Vector2(scene.fog.near, scene.fog.far) },
      uH: { value: G.tex }, uWin: { value: new THREE.Vector3(0, 0, FLAT ? 0 : 1) },   // window corner (x, z), 0 = flat
    },
    vertexShader: `
      uniform sampler2D uH; uniform vec3 uWin;
      varying vec3 vW;
      float cellH(ivec2 c) { return texelFetch(uH, clamp(c, ivec2(0), ivec2(${GN - 1})), 0).r; }
      void main() {
        vec4 w = modelMatrix * vec4(position, 1.0);
        vec2 f = clamp((w.xz - uWin.xy) / ${GCELL.toFixed(1)}, 0.0, ${(GN - 1.001).toFixed(3)});
        ivec2 c = ivec2(floor(f)); vec2 t = f - vec2(c);
        w.y = uWin.z * mix(mix(cellH(c), cellH(c + ivec2(1, 0)), t.x), mix(cellH(c + ivec2(0, 1)), cellH(c + ivec2(1, 1)), t.x), t.y);
        vW = w.xyz; gl_Position = projectionMatrix * viewMatrix * w;
      }`,
    fragmentShader: `
      uniform vec3 uGround, uMinor, uMajor, uFog; uniform vec2 uFogRange;
      varying vec3 vW;
      // Coverage of a square grid of period s (metres) by lines w pixels wide: the distance to the nearest line
      // in pixels comes from the screen-space derivative; a period under ~2-5 px fades out (no moire).
      float grid(vec2 p, float s, float w) {
        vec2 q = p / s, fw = max(fwidth(q), vec2(1e-6));
        vec2 d = abs(fract(q - 0.5) - 0.5) / fw;
        vec2 a = (1.0 - clamp(d - (w - 1.0) * 0.5, 0.0, 1.0)) * (1.0 - smoothstep(0.2, 0.5, fw));
        return max(a.x, a.y);
      }
      void main() {
        vec3 col = mix(uGround, uMinor, grid(vW.xz, 2.0, 1.0));
        col = mix(col, uMajor, grid(vW.xz, 10.0, 1.3));
        col = mix(col, uFog, smoothstep(uFogRange.x, uFogRange.y, distance(vW, cameraPosition)));
        gl_FragColor = vec4(col, 1.0);
        ${colorOut}
      }`,
  }));
ground.frustumCulled = false;
scene.add(ground);
const sky = new THREE.Mesh(
  new THREE.SphereGeometry(1000, 32, 16),
  new THREE.ShaderMaterial({
    side: THREE.BackSide, depthWrite: false, fog: false,
    uniforms: { uHorizon: { value: new THREE.Color(HORIZON) }, uZenith: { value: new THREE.Color(ZENITH) } },
    vertexShader: 'varying vec3 vDir; void main() { vDir = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
    fragmentShader: `
      uniform vec3 uHorizon, uZenith; varying vec3 vDir;
      void main() {
        gl_FragColor = vec4(mix(uHorizon, uZenith, smoothstep(0.0, 0.45, normalize(vDir).y)), 1.0);
        ${colorOut}
      }`,
  }));
sky.renderOrder = -10; sky.frustumCulled = false;
scene.add(sky);
// Both follow the camera, so the world has no edge: the grid is computed from world coordinates.
function followCamera() { sky.position.copy(camera.position); ground.position.set(camera.position.x, 0, camera.position.z); }

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
new THREE.LineLoop(unitCircle).computeLineDistances();  // the dashed ring's line distances, once (the geometry is shared)
const objects = new Map();
// ?rings=0 starts with the error rings hidden (far objects' honest rings span the drone view).
let showRings = params.get('rings') !== '0', showTruth = false;

function shapeFor(c) {
  if (c.group) return null;
  const cls = c.chip ? chipData[c.chip]?.cls : null;
  const coarse = c.mix[0] ? 'person' : null;
  const name = cls === 7 ? 'truck' : cls === 5 ? 'bus' : cls === 1 || cls === 3 ? 'cycle' : cls === 0 || coarse ? 'person' : 'car';
  return name;
}

const TRAIL_MAX = 48;                                // 3 s at 10 Hz, with room
const trailMat = new THREE.LineBasicMaterial({ color: INK2, transparent: true, opacity: 0.7, vertexColors: true });
// The trail of a contact the receiver says is moving, and only that: a parked car's trail is range jitter along
// the line of sight (thin lines towards the horizon). Its points are smoothed (a centred 1-2-3-2-1 average, ends
// renormalised), and its strength is how far it travelled beyond its own error ring (a trail shorter than the ring
// says nothing) times how straight it is (smoothed end-to-end over path length: a zigzag is noise, a track is a line),
// eased per state so a flickering motion flag does not flicker the line.
const TRAIL_KERNEL = [1, 2, 3, 2, 1];
const TRAIL_EASE = 0.3, TRAIL_STRAIGHT = [0.5, 0.85];  // per-state easing; straightness where the trail starts / is fully shown
function updateTrail(o, c) {
  const pts = o.trail, m = pts.length, half = TRAIL_KERNEL.length >> 1;
  let target = 0;
  o.smooth.length = 0;
  if (c.motion === 'moving' && !c.group && !c.lost && m >= 3) {
    for (let k = 0; k < m; k++) {
      let e = 0, n = 0, w = 0;
      for (let d = -half; d <= half; d++) { const q = pts[k + d]; if (q) { const f = TRAIL_KERNEL[d + half]; e += f * q.e; n += f * q.n; w += f; } }
      o.smooth.push({ e: e / w, n: n / w });
    }
    let path = 0;
    for (let k = 1; k < m; k++) path += Math.hypot(o.smooth[k].e - o.smooth[k - 1].e, o.smooth[k].n - o.smooth[k - 1].n);
    const net = Math.hypot(o.smooth[m - 1].e - o.smooth[0].e, o.smooth[m - 1].n - o.smooth[0].n), r = o.cur.r;
    const far = clamp((net - r) / (r + 3), 0, 1);
    const straight = clamp((net / Math.max(path, 1e-6) - TRAIL_STRAIGHT[0]) / (TRAIL_STRAIGHT[1] - TRAIL_STRAIGHT[0]), 0, 1);
    target = far * straight;
  }
  o.trailK += (target - o.trailK) * TRAIL_EASE;
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
    // The trail: a preallocated line, rewritten every frame (history of the displayed positions + the live one).
    const tg = new THREE.BufferGeometry();
    tg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(3 * (TRAIL_MAX + 1)), 3).setUsage(THREE.DynamicDrawUsage));
    tg.setAttribute('color', new THREE.BufferAttribute(new Float32Array(4 * (TRAIL_MAX + 1)), 4).setUsage(THREE.DynamicDrawUsage));   // rgb 1, alpha fades to the tail
    tg.setDrawRange(0, 0);
    const rg = new THREE.BufferGeometry().setAttribute('position', unitCircle.attributes.position.clone());   // own copy: draped on the ground each frame
    rg.setAttribute('lineDistance', unitCircle.attributes.lineDistance.clone());
    ring.geometry = rg;
    const trailLine = new THREE.Line(tg, trailMat); trailLine.frustumCulled = false;
    scene.add(trailLine);
    o = { root, ring, tag, el, body: null, bodyKey: '', trail: [], smooth: [], trailK: 0, trailLine, prev: null, cur: null };
    objects.set(c.id, o);
  }
  // The body: the chip once it arrived (and its data loaded), else the class stand-in; a group is a ring and a count.
  const haveChip = !!(c.chip && chipData[c.chip]);
  const key = c.group ? 'group' : haveChip ? `chip:${c.chip}` : `stand:${shapeFor(c)}`;
  if (key !== o.bodyKey) {
    if (o.body) o.root.remove(o.body);
    o.body = null;
    if (haveChip) {
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

// Where a contact stands. `u` is the object's CENTRE height above the u = 0 plane, as sent; its base is
// H_C[class] below that. No height sent (null): it rides the ground, centre at H_C (placeObjects).
function placeOf(c, chip) {
  const cls = chip ? chip.cls ?? 2 : c.mix[0] ? 0 : 2;
  const h = H_C[cls] ?? 0.75;
  const u = c.u ?? h;
  return { u, base: u - h, h, sent: c.u != null };
}
// What a contact tells the ground: its base, weighted by how much the receiver still vouches for it (a lost contact
// stood on real ground but may have been misplaced; a group's base is its members' mean) and by its position error
// (a far contact's height is as uncertain as its range). The memory is per contact id, replaced as it moves.
const G_LIVE_W = { fresh: 1, unheard: 0.6 };
function groundSample(c, p, t) {
  if (FLAT || !G.set || !p.sent) return;
  const x = c.e, z = -c.n, i = Math.round((x - G.x0) / GCELL), j = Math.round((z - G.z0) / GCELL);
  if (i < 0 || j < 0 || i >= GN || j >= GN) return;
  const w = (G_LIVE_W[c.liveness] ?? 0.3) * (c.group ? 0.5 : 1) / (1 + (c.ceShown / 4) ** 2);
  G.samples.set(c.id, { i, j, x, z, h: p.base, w, t });
}
// Yaw (degrees true): course while it moves (> 1.5 m/s), else the heading that crossed the link, else the
// chip's own orientation, else the course (or north).
function yawOf(c, chip) {
  if (c.motion === 'moving' && c.speed > 1.5) return c.course;
  if (c.heading != null) return c.heading;
  return chip ? chip.yaw0 : c.course || 0;
}

// A state arrived: each contact's target (cur) and where it was shown last (prev). New contacts snap.
function updateContacts(list, t, snap) {
  const live = new Set();
  for (const c of list) {
    live.add(c.id);
    const o = contactObject(c);
    const chip = c.chip ? chipData[c.chip] : null;
    const pl = placeOf(c, chip), { u, base } = pl;
    groundSample(c, pl, t);
    // The error ring: what the receiver can vouch for (ce_shown), on the ground.
    const r = Math.max(0.6, c.group ? Math.max(c.radius, c.ceShown) : c.ceShown);
    const cur = { t, e: c.e, n: c.n, u, base, h: pl.h, sent: pl.sent, yaw: yawOf(c, chip), r };
    o.prev = !o.cur || snap ? cur : o.cur;
    o.cur = cur;
    if (snap) { o.trail.length = 0; o.trailK = 0; }
    // The trail keeps where the object was shown (the previous state); the live head is added per frame.
    o.trail.push(o.prev);
    while (o.trail.length > 1 && (t - o.trail[0].t > 3 || o.trail.length > TRAIL_MAX)) o.trail.shift();
    updateTrail(o, c);
    const fresh = c.liveness === 'fresh';
    if (o.body) {
      const op = fresh ? (c.chip ? 1 : 0.85) : c.liveness === 'unheard' ? 0.55 : 0.3;
      if (o.body.material.uniforms) o.body.material.uniforms.uOpacity.value = op; else o.body.material.opacity = op;
    }
    o.ring.visible = showRings;
    o.ring.material = c.lost || !fresh ? ringLost : ringMat;
    const name = c.group ? `${c.count} ${c.mix[0] >= c.mix[1] ? 'dismounts' : 'vehicles'}` : chip ? CLASS[chip.cls] : c.mix[0] ? 'dismount' : 'vehicle';
    o.el.textContent = `${name}${c.motion === 'moving' ? ` ${c.speed.toFixed(1)} m/s` : ''}${c.chip ? '' : c.group ? '' : ' · chip pending'}`;
    o.el.className = fresh ? 'tag' : 'tag dim';
    o.tag.position.set(0, 1.6, 0);
    // Labels only on hover (docs/STYLE.md), and on groups, which have no body to read.
    o.tag.visible = c.group || hovered === c.id;
  }
  for (const [id, o] of objects) if (!live.has(id)) removeObject(id, o);
}
function removeObject(id, o) {
  scene.remove(o.root); scene.remove(o.ring); scene.remove(o.trailLine); o.trailLine.geometry.dispose();
  o.el.remove(); objects.delete(id);
}
// The display at alpha (0 = the previous state, 1 = the current one): positions and heights linearly,
// angles the short way round. Each object stands on the ground field under it (standOn), and its ring and trail are
// laid on that same ground, vertex by vertex, a few cm above it (a little more with range: the depth buffer).
const lift = (x, z) => 0.12 + 0.001 * Math.hypot(x - camera.position.x, z - camera.position.z);
function placeObjects(a) {
  for (const [, o] of objects) {
    const p = o.prev, q = o.cur;
    const e = lerp(p.e, q.e, a), n = lerp(p.n, q.n, a), u = lerp(p.u, q.u, a), sent = lerp(p.base, q.base, a), h = u - sent;
    // The ground under it; an object without a height rides it. `off` is how far above the ground it stands.
    const g = groundAt(e, -n), off = q.sent ? standOn(sent - g) : 0, base = g + off;
    o.root.position.set(e, base + h, -n);
    o.root.rotation.y = -lerpAng(p.yaw, q.yaw, a) * Math.PI / 180;
    const r = lerp(p.r, q.r, a);
    o.ring.position.set(e, base + lift(e, -n), -n);
    o.ring.scale.set(r, 1, r);
    if (o.ring.visible) {                           // draped: a 20 m ring on a 4 % slope spans 0.8 m of height
      const rp = o.ring.geometry.attributes.position, ry = o.ring.position.y;
      for (let k = 0; k < rp.count; k++) rp.setY(k, r < 2 ? 0 : groundAt(e + rp.getX(k) * r, -n + rp.getZ(k) * r) + off + lift(e, -n) - ry);
      rp.needsUpdate = true;
    }
    drawTrail(o, e, n, off);
  }
}
// The smoothed history plus the live head, on the ground, fading to the tail; nothing while the contact is not moving.
function drawTrail(o, e, n, off) {
  const geo = o.trailLine.geometry, pos = geo.attributes.position, col = geo.attributes.color, sm = o.smooth;
  if (o.trailK < 0.03 || sm.length < 2) { geo.setDrawRange(0, 0); return; }
  const m = sm.length;
  for (let k = 0; k <= m; k++) {
    const x = k < m ? sm[k].e : e, z = -(k < m ? sm[k].n : n);
    pos.setXYZ(k, x, groundAt(x, z) + off + lift(x, z) + 0.02, z);
    col.setXYZW(k, 1, 1, 1, o.trailK * (k / m) ** 1.5);
  }
  pos.needsUpdate = col.needsUpdate = true;
  geo.setDrawRange(0, m + 1);
}

// ---- the drone ---------------------------------------------------------------------------------------
const frustum = new THREE.LineSegments(new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(new Float32Array(3 * 16), 3)),
  new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: 0.9 }));
frustum.frustumCulled = false;
const nadir = new THREE.Line(new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3)),
  new THREE.LineDashedMaterial({ color: INK3, dashSize: 0.6, gapSize: 0.5 }));
nadir.frustumCulled = false;
scene.add(frustum, nadir);
// The camera's orientation from a pose (degrees): yaw is the compass bearing of the optical axis, pitch
// negative looks down, roll about the axis. Right-handed in three's frame too (x east, y up, z south).
function camBasis(p) {
  const yaw = p.yaw * Math.PI / 180, pitch = p.pitch * Math.PI / 180, roll = (p.roll || 0) * Math.PI / 180;
  const fwd = v3(Math.sin(yaw) * Math.cos(pitch), Math.cos(yaw) * Math.cos(pitch), Math.sin(pitch));
  let right = v3(Math.cos(yaw), -Math.sin(yaw), 0);
  let up = new THREE.Vector3().crossVectors(right, fwd);
  if (roll) { right.applyAxisAngle(fwd, -roll); up.applyAxisAngle(fwd, -roll); }
  return { fwd, right, up };
}
function updateDrone(p) {
  // In the drone's own view the frustum would be the frame's border and the nadir line is out of frame.
  const show = !!p && view !== 'drone';
  frustum.visible = nadir.visible = show;
  if (!show) return;
  const c = v3(p.e, p.n, p.u), { fwd, right, up } = camBasis(p);
  const d = 10, hw = d * Math.tan((hfov / 2) * Math.PI / 180), hh = hw / imgAspect;
  const corners = [[1, 1], [-1, 1], [-1, -1], [1, -1]].map(([x, y]) => c.clone().addScaledVector(fwd, d).addScaledVector(right, x * hw).addScaledVector(up, y * hh));
  const seg = [];
  for (const k of corners) seg.push(c, k);
  for (let k = 0; k < 4; k++) seg.push(corners[k], corners[(k + 1) % 4]);
  const pos = frustum.geometry.attributes.position;
  seg.forEach((s, k) => pos.setXYZ(k, s.x, s.y, s.z));
  pos.needsUpdate = true;
  // The plumb line down to the ground under the drone.
  const np = nadir.geometry.attributes.position;
  np.setXYZ(0, c.x, c.y, c.z); np.setXYZ(1, c.x, groundAt(c.x, c.z), c.z); np.needsUpdate = true; nadir.computeLineDistances();
}

// ---- edge truth (what the edge tracked; not what crossed the link) ----------------------------------
const truthMat = new THREE.PointsMaterial({ color: INK3, size: 0.5, sizeAttenuation: true });
const truthGeo = new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(new Float32Array(3 * 2048), 3).setUsage(THREE.DynamicDrawUsage));
const truth = new THREE.Points(truthGeo, truthMat);
truth.frustumCulled = false;
scene.add(truth);
let truthPrev = new Map(), truthCur = new Map();
function updateTruth(tracks, snap) {
  truthPrev = snap ? null : truthCur;
  truthCur = new Map(tracks.map((x) => [x.id, [x.e, x.u + 1.4, -x.n]]));
  if (!truthPrev) truthPrev = truthCur;
}
function placeTruth(a) {
  truth.visible = showTruth;
  if (!showTruth) return;
  const pos = truthGeo.attributes.position;
  let k = 0;
  for (const [id, q] of truthCur) {
    if (k >= 2048) break;
    const p = truthPrev.get(id) ?? q;
    pos.setXYZ(k++, lerp(p[0], q[0], a), lerp(p[1], q[1], a), lerp(p[2], q[2], a));
  }
  pos.needsUpdate = true; truthGeo.setDrawRange(0, k);
}

// ---- views -----------------------------------------------------------------------------------------
const VIEWS = ['chase', 'drone', 'orbit', 'top'];
let view = VIEWS.includes(params.get('view')) ? params.get('view') : 'chase';
let lastPose = null;                                 // the received pose (state), and the interpolated one on screen
let posePrev = null, poseCur = null, dispPose = null;
$('views').innerHTML = VIEWS.map((v) => `<button data-v="${v}">${v}</button>`).join('');
$('views').onclick = (ev) => { const v = ev.target.dataset?.v; if (v) { view = v; setFov(); placeView(true); markButtons(); } };
function placeView(jump) {
  const p = dispPose;
  if (!p) return;
  const c = v3(p.e, p.n, p.u), { fwd, up } = camBasis(p);
  const look = c.clone().addScaledVector(fwd, 60);
  if (view === 'drone') { camera.position.copy(c); camera.up.copy(up); camera.lookAt(look); controls.enabled = false; return; }
  camera.up.set(0, 1, 0); controls.enabled = true;
  if (!jump) return;
  if (view === 'chase') {
    // Above and behind the drone, looking at where its camera looks on the ground.
    const dep = Math.max(8, -p.pitch) * Math.PI / 180;
    const reach = Math.min(150, Math.max(p.u - groundAt(c.x, c.z), 1) / Math.tan(dep));
    const flat = fwd.clone().setY(0).normalize();
    const aim = v3(p.e, p.n, 0).addScaledVector(flat, reach * 0.8); aim.y = groundAt(aim.x, aim.z);
    camera.position.copy(c).addScaledVector(flat, -30).add(new THREE.Vector3(0, 32, 0)); controls.target.copy(aim);
  }
  if (view === 'orbit') { camera.position.copy(look).add(new THREE.Vector3(70, 60, 70)); controls.target.copy(look); }
  if (view === 'top') { camera.position.copy(look).add(new THREE.Vector3(0, 140, 0.01)); controls.target.copy(look); }
  controls.update();
}

// ---- the frame: everything shown at alpha between the previous and the current state ----------------
let groundSnap = false;                              // the next frame shows the new ground at once (a seek, a scene switch)
let tArrive = 0, alphaFreeze = null, tPrev = 0, tCur = 0, footageS = 0, timeShown = '';
const autoAlpha = () => clamp((performance.now() - tArrive) / STEP_MS, 0, 1);
function apply(a) {
  if (posePrev && poseCur) {
    dispPose = {
      e: lerp(posePrev.e, poseCur.e, a), n: lerp(posePrev.n, poseCur.n, a), u: lerp(posePrev.u, poseCur.u, a),
      yaw: lerpAng(posePrev.yaw, poseCur.yaw, a), pitch: lerpAng(posePrev.pitch, poseCur.pitch, a), roll: lerpAng(posePrev.roll || 0, poseCur.roll || 0, a),
    };
  } else dispPose = poseCur;
  setFov();
  if (!FLAT) easeGround(lerp(tPrev, tCur, a), groundSnap);
  groundSnap = false;
  updateDrone(dispPose);
  if (view === 'drone') placeView(false);
  placeObjects(a);
  placeTruth(a);
  // The clock shows the time on screen (between the two states), not the newest state's.
  const tt = `${lerp(tPrev, tCur, a).toFixed(1)} / ${footageS.toFixed(1)} s`;
  if (tt !== timeShown) $('vTime').textContent = timeShown = tt;
}
function draw() {
  if (view !== 'drone') controls.update();
  followCamera();
  renderer.render(scene, camera);
  labels.render(scene, camera);
}
function frame() {
  apply(alphaFreeze ?? autoAlpha());
  draw();
  requestAnimationFrame(frame);
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
$('bRings').onclick = () => { showRings = !showRings; for (const [, o] of objects) o.ring.visible = showRings; markButtons(); };
function markButtons() {
  for (const b of $('profiles').children) b.classList.toggle('on', b.dataset.p === state?.profile);
  for (const b of $('views').children) b.classList.toggle('on', b.dataset.v === view);
  for (const b of $('scenes').children) b.classList.toggle('on', b.dataset.s === state?.scene);
  $('bTruth').classList.toggle('on', showTruth); $('bRings').classList.toggle('on', showRings);
  $('bPlay').textContent = state?.playing === false ? 'Play' : 'Pause';
}

// The drone pose the receiver holds: the camera Pose stream while video flows; otherwise Ego (position,
// heading, altitude) with the pitch from where the optical axis meets the ground (the footprint centre
// Ego carries), else the last pitch we had.
function receivedPose(s) {
  if (s.rx.pose && s.video) return s.rx.pose;
  const g = s.rx.ego;
  if (!g) return lastPose;
  let pitch = lastPose?.pitch ?? -15;
  const reach = Math.hypot((g.fpE ?? g.e) - g.e, (g.fpN ?? g.n) - g.n), pf = -Math.atan2(Math.max(g.altAgl, 1), reach) * 180 / Math.PI;
  if (g.fpE != null && reach > 2 && pf < -5 && pf > -85) pitch = pf;
  return { e: g.e, n: g.n, u: g.altAgl, yaw: g.heading, pitch, roll: 0 };
}

const fmtB = (b) => (b >= 1000 ? `${(b / 1000).toFixed(1)} kB` : `${b} B`);
let firstState = true;
const waiting = new Map();                           // request id -> resolver, for window.minband.cmd
ws.onmessage = (ev) => {
  const prev = state;
  state = JSON.parse(ev.data);
  const s = state;
  // A seek, a restart, a scene switch or a long gap: nothing to interpolate from, everything snaps.
  const snap = !prev || s.scene !== prev.scene || s.t < prev.t - 1e-6 || s.t - prev.t > 3.5 * STEP_MS / 1000;
  if (s.scene !== sceneName) { loadScene(s.scene); $('scenes').innerHTML = s.scenes.map((n) => `<button data-s="${n}">${n}</button>`).join(''); markButtons(); }
  lastPose = receivedPose(s);
  posePrev = !poseCur || snap ? lastPose : poseCur;
  poseCur = lastPose;
  if (snap) { G.samples.clear(); G.jump = true; groundSnap = true; G.tBuilt = -1e9; }
  // The ground window is centred on the first pose of a scene (the drone and everything it reports is within ~1 km).
  if (!G.set && lastPose) {
    G.set = true;
    G.x0 = Math.round((lastPose.e - GN * GCELL / 2) / GCELL) * GCELL; G.z0 = Math.round((-lastPose.n - GN * GCELL / 2) / GCELL) * GCELL;
    ground.material.uniforms.uWin.value.set(G.x0, G.z0, FLAT ? 0 : 1);
  }
  updateContacts(s.rx.contacts, s.t, snap);
  if (!FLAT && G.set && (snap || s.t - G.tBuilt >= G_REBUILD_S || s.t < G.tBuilt)) rebuildGround(s.t, snap);
  updateTruth(s.edge.tracks, snap);
  tPrev = snap ? s.t : tCur; tCur = s.t; footageS = s.footageS;
  tArrive = performance.now();
  apply(alphaFreeze ?? 0);
  if (firstState && dispPose) { placeView(true); firstState = false; }
  // The video plays along and is re-seeked only on real drift: a seek every step never completes.
  if (!REC) {
    if (s.video) {
      if (s.playing && video.paused) video.play().catch(() => {});
      if (!s.playing && !video.paused) video.pause();
      if (Math.abs(video.currentTime - s.t) > 0.4 && !video.seeking) video.currentTime = Math.min(s.t, s.footageS);
      drawBoxes(s.edge.tracks);
    } else if (!video.paused) video.pause();
    $('videoBox').classList.toggle('down', !s.video);
  }
  const budget = s.budgetBps ? `${(s.budgetBps / 1000).toFixed(1)} kbit/s` : 'video link';
  $('vWire').textContent = `${fmtB(s.wire.bytesPerS)}/s · ${budget}`;
  $('vTwin').textContent = `${s.rx.known}${s.rx.of >= 0 ? ` of ${s.rx.of}` : ''} known · lvl ${s.edge.level}`;
  $('vChips').textContent = `${s.wire.chipsDelivered} in · ${s.wire.chipQueue} queued · ${fmtB(s.wire.chipBytes)}`;
  $('events').innerHTML = s.rx.events.slice().reverse().map((e) => `<div><span>${e.t.toFixed(1)}</span>${e.text}</div>`).join('');
  if (!prev || prev.profile !== s.profile || prev.playing !== s.playing) markButtons();
  if (s.ack != null && waiting.has(s.ack)) { waiting.get(s.ack)(s); waiting.delete(s.ack); }
};

$('note').innerHTML = 'Contacts, heights, headings and the drone pose cross the link as MinBand frames (real WASM edge, shaped link, WASM receiver). '
  + 'The reconstruction map stays on the drone: it only places the objects in 3D. The ground here is a simulation, a coarse height field built from the contacts\' own base heights. '
  + 'Chips travel a simulated channel at half the link with their real compressed size; the core does not put ChipSym records on the wire yet.';

// ---- scenes ------------------------------------------------------------------------------------------
// A scene switch (or the first state) loads that scene's camera, chips and video; there is no map.
let sceneName = null, loadedP = Promise.resolve();
function loadScene(name) {
  sceneName = name;
  for (const [id, o] of [...objects]) removeObject(id, o);
  chipTex.clear(); chipData = {};
  posePrev = poseCur = dispPose = lastPose = null;
  G.set = false; G.samples.clear(); G.jump = true; groundSnap = true; G.tBuilt = -1e9;
  const q = `?s=${encodeURIComponent(name)}`;
  loadedP = Promise.all([
    fetch(`/summary.json${q}`).then((r) => r.json()).then((s) => { hfov = s.hfov_deg; imgAspect = s.width / s.height; setFov(); }),
    fetch(`/chips.json${q}`).then((r) => r.json()).then((c) => { if (sceneName === name) chipData = c; }),
  ]).then(() => {});
  if (!REC) video.src = `/video.mp4${q}`;
  firstState = true;
}

// How well the ground fits what crossed the link: for every live contact with a height, |ground - its base| on the
// u = 0 plane (the old ground), on the shown field (it includes the contact itself), and on the field rebuilt without
// that contact (leave-one-out: what the ground would say about an object it had not heard of). [median, p90] metres,
// for all contacts with a height and for the well-located ones (ring <= 3 m).
function groundStats() {
  const med = (a, f) => { a = a.slice().sort((x, y) => x - y); return a.length ? +a[Math.floor((a.length - 1) * f)].toFixed(2) : null; };
  const out = {};
  for (const [name, keep] of [['all', () => true], ['ring<=3m', (r) => r <= 3]]) {
    const flat = [], shown = [], loo = [];
    for (const [id, o] of objects) {
      const q = o.cur;
      if (!q.sent || !G.samples.has(id) || !keep(q.r)) continue;
      flat.push(Math.abs(q.base));
      shown.push(Math.abs(groundAt(q.e, -q.n) - q.base));
      const pts = groundPoints(id);
      loo.push(Math.abs(groundIdw(pts, groundPlane(pts), q.e, -q.n) - q.base));
    }
    out[name] = { n: flat.length, flat: [med(flat, 0.5), med(flat, 0.9)], field: [med(shown, 0.5), med(shown, 0.9)], leaveOneOut: [med(loo, 0.5), med(loo, 0.9)] };
  }
  return out;
}

// ---- for scripts: screenshot.mjs --eval, and record.mjs (CLOCK=manual) -------------------------------
// cmd(m) sends a command to the driver and resolves with the state (its answer, matched by id) once applied;
// step(n) / seek(t) are that for {cmd:'step'} / {cmd:'seek'}. renderAt(alpha) freezes the display at alpha
// (0 = the previous state, 1 = the current) and renders one frame synchronously; freezeAlpha(null) releases it.
function cmd(m, timeoutMs = 20000) {
  return new Promise((ok, fail) => {
    const id = Math.floor(Math.random() * 2 ** 31);
    const timer = setTimeout(() => { waiting.delete(id); fail(new Error(`no answer to ${JSON.stringify(m)}`)); }, timeoutMs);
    waiting.set(id, (s) => { clearTimeout(timer); ok(s); });
    const go = () => ws.send(JSON.stringify({ ...m, id }));
    if (ws.readyState === 1) go(); else ws.addEventListener('open', go, { once: true });
  });
}
window.minband = {
  THREE, camera, controls, objects, v3, get state() { return state; }, get pose() { return dispPose; }, get hfov() { return hfov; },
  groundAt, groundStats,
  setView: (v) => { view = v; setFov(); placeView(true); markButtons(); },
  cmd, step: (n = 1) => cmd({ cmd: 'step', n }), seek: (t) => cmd({ cmd: 'seek', t }), whenLoaded: () => loadedP,
  freezeAlpha: (a) => { alphaFreeze = a == null ? null : clamp(a, 0, 1); },
  renderAt: (a) => { alphaFreeze = clamp(a, 0, 1); apply(alphaFreeze); draw(); },
};
resize();
markButtons();
frame();
