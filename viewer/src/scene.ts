// The world twin (docs/STYLE.md). Wire is the environment: one procedural contour field at --ink-4,
// device frustums, trails and axes as 1 px lines (device = line style). Fill is what the twin
// believes exists: entities are matte monochrome solids under soft hemispheric light (class =
// silhouette: person capsule, carried sphere, static box). Staleness decays: bright solid -> the
// same solid fading toward --ink-3 -> dotted outline -> gone. Restraint: entities are the brightest
// thing on screen, trails last 3 s, labels appear only on hover or tap, the terrain never moves on
// its own, and only a lost device blinks.
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { CSS2DObject, CSS2DRenderer } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import type { GlobalEntity, Snapshot } from './types';

// ---- tokens (single source: the CSS variables in index.html) ----------------------------------
const token = (name: string, fallback: string) =>
  new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback);
const BG = token('--bg', '#070809');
const INK = token('--ink', '#e9ecef');
const INK2 = token('--ink-2', '#9aa3ad');
const INK3 = token('--ink-3', '#4a525b');
const INK4 = token('--ink-4', '#1a1e23');

/** 1 Hz blink, 40..100 % opacity, phase-locked to the CSS `blink` animation. Lost devices only. */
export const blinkAt = (ms: number) => 0.7 + 0.3 * Math.cos((2 * Math.PI * (ms % 1000)) / 1000);

/** Device line styles, assigned in order of first sight. Dash lengths in metres. */
export const LINE_STYLES = [
  { name: 'solid', dash: 0, gap: 0, svg: '' },
  { name: 'dashed', dash: 0.14, gap: 0.09, svg: '6 3' },
  { name: 'dotted', dash: 0.02, gap: 0.07, svg: '1 3' },
] as const;

type Kind = 'person' | 'carried' | 'static';
// COCO ids. Person = capsule, things people carry = small sphere, everything else = box.
const CARRIED = new Set([24, 25, 26, 27, 28, 39, 40, 41, 42, 43, 44, 64, 65, 67, 73, 76, 79]);
const kindOf = (cls: number): Kind => (cls === 0 ? 'person' : CARRIED.has(cls) ? 'carried' : 'static');
const CLASS_NAME: Record<number, string> = {
  0: 'person', 24: 'backpack', 25: 'umbrella', 26: 'handbag', 28: 'suitcase', 39: 'bottle', 41: 'cup',
  56: 'chair', 57: 'couch', 58: 'plant', 59: 'bed', 60: 'table', 62: 'tv', 63: 'laptop', 67: 'phone', 73: 'book',
};
const className = (cls: number) => CLASS_NAME[cls] ?? `class ${cls}`;

// ---- materials -------------------------------------------------------------------------------
const additive = { transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, fog: false } as const;
function lineMat(style = 0, opacity = 1, vertexColors = false): THREE.LineBasicMaterial {
  const s = LINE_STYLES[style % LINE_STYLES.length];
  const p = { ...additive, color: INK, opacity, vertexColors };
  return s.dash ? new THREE.LineDashedMaterial({ ...p, dashSize: s.dash, gapSize: s.gap }) : new THREE.LineBasicMaterial(p);
}
const dottedMat = (opacity = 1) => new THREE.LineDashedMaterial({ ...additive, color: INK, opacity, dashSize: 0.018, gapSize: 0.045 });

// ---- geometry builders -----------------------------------------------------------------------
const segs = (p: number[]) => { const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.Float32BufferAttribute(p, 3)); return g; };
/** LineDashedMaterial needs per-vertex distances; shared geometries get them once. */
const dashed = (g: THREE.BufferGeometry) => { new THREE.LineSegments(g).computeLineDistances(); return g; };

type Profile = (s: number) => [number, number]; // s in [0,1] bottom pole -> top pole: [radius, y]
const capsuleProfile = (r: number, len: number): Profile => s => {
  const q = (Math.PI * r) / 2;
  let d = s * (2 * q + len);
  if (d < q) { const t = -Math.PI / 2 + d / r; return [r * Math.cos(t), -len / 2 + r * Math.sin(t)]; }
  d -= q;
  if (d < len) return [r, -len / 2 + d];
  const t = (d - len) / r;
  return [r * Math.cos(t), len / 2 + r * Math.sin(t)];
};

/** Lines of a surface of revolution: meridians along the profile and rings at the given s. */
function revolve(profile: Profile, rings: number[], meridians: number, steps = 28, seg = 36, twist = 0): THREE.BufferGeometry {
  const p: number[] = [];
  for (let m = 0; m < meridians; m++) {
    const a = (m / meridians) * Math.PI * 2 + twist, ca = Math.cos(a), sa = Math.sin(a);
    for (let k = 0; k < steps; k++) {
      const [r0, y0] = profile(k / steps), [r1, y1] = profile((k + 1) / steps);
      p.push(r0 * ca, y0, r0 * sa, r1 * ca, y1, r1 * sa);
    }
  }
  for (const s of rings) {
    const [r, y] = profile(s);
    if (r < 1e-3) continue;
    for (let k = 0; k < seg; k++) {
      const a0 = (k / seg) * Math.PI * 2, a1 = ((k + 1) / seg) * Math.PI * 2;
      p.push(r * Math.cos(a0), y, r * Math.sin(a0), r * Math.cos(a1), y, r * Math.sin(a1));
    }
  }
  return segs(p);
}
interface KindGeo { solid: THREE.BufferGeometry; outline: THREE.BufferGeometry; half: number }
let KIND: Record<Kind, KindGeo> | null = null;
function kinds(): Record<Kind, KindGeo> {
  if (KIND) return KIND;
  KIND = {
    person: { solid: new THREE.CapsuleGeometry(0.2, 1.3, 8, 24), outline: dashed(revolve(capsuleProfile(0.2, 1.3), [0.12, 0.5, 0.88], 4, 28, 36, Math.PI / 4)), half: 0.85 },
    carried: { solid: new THREE.SphereGeometry(0.14, 28, 18), outline: dashed(revolve(capsuleProfile(0.14, 0), [0.5], 3, 20, 28)), half: 0.14 },
    static: { solid: new THREE.BoxGeometry(0.4, 0.4, 0.4), outline: dashed(new THREE.EdgesGeometry(new THREE.BoxGeometry(0.4, 0.4, 0.4))), half: 0.2 },
  };
  return KIND;
}

// ---- terrain: a displaced plane drawn as one quad-wire contour layer at --ink-4 -------------
function hash2(x: number, z: number): number {
  let h = (Math.imul(x, 374761393) + Math.imul(z, 668265263)) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}
function vnoise(x: number, z: number): number {
  const ix = Math.floor(x), iz = Math.floor(z), fx = x - ix, fz = z - iz;
  const u = fx * fx * (3 - 2 * fx), v = fz * fz * (3 - 2 * fz);
  const a = hash2(ix, iz), b = hash2(ix + 1, iz), c = hash2(ix, iz + 1), d = hash2(ix + 1, iz + 1);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}
function fbm(x: number, z: number, oct: number, ridge = false): number {
  let s = 0, amp = 0.5, f = 1, norm = 0;
  for (let o = 0; o < oct; o++) {
    let n = vnoise(x * f + o * 17.3, z * f - o * 9.1);
    if (ridge) n = 1 - Math.abs(2 * n - 1);
    s += amp * n; norm += amp; amp *= 0.5; f *= 2.03;
  }
  return s / norm;
}
const smooth = (a: number, b: number, x: number) => { const t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
/** Flat basin around the marker, low swells, a ridge line far out. Metres. */
function terrainHeight(x: number, z: number): number {
  const r = Math.hypot(x, z);
  const swell = smooth(6, 15, r) * 1.1 * (fbm(x * 0.08, z * 0.08, 4) - 0.3);
  const ridge = Math.pow(smooth(13, 40, r), 1.3) * 7.5 * Math.pow(fbm(x * 0.045 + 3.1, z * 0.045 - 1.7, 5, true), 2.2);
  return Math.max(0, swell) + ridge;
}

function buildTerrain(): THREE.LineSegments {
  const S = 104, N = 104, st = S / N, W = N + 1;
  const X = (i: number) => -S / 2 + i * st;
  const H = new Float32Array(W * W);
  for (let j = 0; j < W; j++) for (let i = 0; i < W; i++) H[j * W + i] = terrainHeight(X(i), X(j)) - 0.004;
  // Quad wire (rows and columns, no diagonals): the one contour layer.
  const p: number[] = [];
  for (let j = 0; j < W; j++) for (let i = 0; i < N; i++) {
    p.push(X(i), H[j * W + i], X(j), X(i + 1), H[j * W + i + 1], X(j));
    p.push(X(j), H[i * W + j], X(i), X(j), H[(i + 1) * W + j], X(i + 1));
  }
  return new THREE.LineSegments(segs(p), new THREE.LineBasicMaterial({ color: INK4, fog: true }));
}

/** The physical marker (white sheet) plus monochrome axes. */
function buildOrigin(): THREE.Group {
  const g = new THREE.Group();
  const marker = new THREE.Mesh(new THREE.PlaneGeometry(0.42, 0.3), new THREE.MeshBasicMaterial({ color: INK, side: THREE.DoubleSide, fog: false }));
  marker.rotation.x = -Math.PI / 2; marker.position.y = 0.003; g.add(marker);
  const ax = new THREE.LineSegments(segs([0, 0.004, 0, 1, 0.004, 0, 0, 0.004, 0, 0, 0.004, 1]), new THREE.LineBasicMaterial({ ...additive, color: INK2, opacity: 0.8 }));
  const up = new THREE.LineSegments(dashed(segs([0, 0, 0, 0, 1, 0])), dottedMat(0.7));
  g.add(ax, up);
  return g;
}

/** Camera frustum: apex at the device, far rectangle 1.2 m out along -Z, an up-triangle on top. */
function frustumGeo(): THREE.BufferGeometry {
  const d = 1.2, hh = d * Math.tan(Math.PI / 6), hw = hh * (4 / 3);
  const c = [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]];
  const p: number[] = [];
  for (let k = 0; k < 4; k++) { const [x, y] = c[k], [x2, y2] = c[(k + 1) % 4]; p.push(0, 0, 0, x, y, -d, x, y, -d, x2, y2, -d); }
  p.push(-hw * 0.4, hh * 1.06, -d, 0, hh * 1.36, -d, 0, hh * 1.36, -d, hw * 0.4, hh * 1.06, -d);
  return dashed(segs(p));
}

// ---- entities --------------------------------------------------------------------------------
interface Ent {
  gid: string; cls: number; kind: Kind;
  group: THREE.Group; body: THREE.Group;
  solid: THREE.Mesh; outline: THREE.LineSegments;
  arrow: THREE.LineSegments; drop: THREE.Line; trail: THREE.Line; trailStyle: number;
  label: CSS2DObject; labelText: string; labelOpacity: number;
  pts: { p: THREE.Vector3; t: number }[];
  staleSince: number | null; dying: boolean;
  a: Record<Channel, number>;
}
// `tone` is 0 for --ink (fresh) .. 1 for --ink-3 (about to lose its body).
type Channel = 'solid' | 'tone' | 'outline' | 'arrow' | 'drop' | 'trail';
const CHANNELS: Channel[] = ['solid', 'tone', 'outline', 'arrow', 'drop', 'trail'];
const TRAIL_MS = 3000, TRAIL_MAX = 120;
const ARROW_MIN = 0.3; // m/s
// Decay schedule, seconds since the entity went stale: solid fading to --ink-3, then dotted outline, then gone.
const SOLID_S = 3, OUTLINE_S = 8;

function targets(e: Ent, now: number): Record<Channel, number> {
  const zero = { solid: 0, tone: 1, outline: 0, arrow: 0, drop: 0, trail: 0 };
  if (e.dying) return zero;
  if (e.staleSince === null) return { solid: 0.95, tone: 0, outline: 0, arrow: 0.7, drop: 0.5, trail: 0.6 };
  const s = (now - e.staleSince) / 1000;
  if (s < SOLID_S) return { solid: 0.88, tone: s / SOLID_S, outline: 0, arrow: 0, drop: 0.3, trail: 0.3 };
  if (s < OUTLINE_S) return { solid: 0, tone: 1, outline: 0.75, arrow: 0, drop: 0.2, trail: 0.12 };
  return zero;
}

interface Frustum { group: THREE.Group; line: THREE.LineSegments; label: CSS2DObject; style: number; silent: boolean }

export class TwinScene {
  private renderer: THREE.WebGLRenderer;
  private labels = new CSS2DRenderer();
  private scene = new THREE.Scene();
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private entities = new Map<string, Ent>();
  private dying: Ent[] = [];
  private frustums = new Map<string, Frustum>();
  private ghosts = new Map<string, { group: THREE.Group; sphere: THREE.LineSegments; stem: THREE.Line; style: number }>();
  private styles = new Map<string, number>();
  private ghostSphere = dashed(revolve(capsuleProfile(0.09, 0), [0.3, 0.5, 0.7], 4, 16, 24));
  private frustumGeometry = frustumGeo();
  private lastT = performance.now();
  /** What the pointer is over (hover) or what was tapped (pinned): its label is the only one shown. */
  private hovered: object | null = null;
  private pinned: object | null = null;
  private pointer: { x: number; y: number } | null = null;
  showGhosts = false;

  constructor(private container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    container.appendChild(this.renderer.domElement);
    Object.assign(this.labels.domElement.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
    container.appendChild(this.labels.domElement);

    this.camera = new THREE.PerspectiveCamera(40, 1, 0.1, 300);
    this.camera.position.set(7.4, 3.9, 9.2);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(0, 0.7, 0);
    Object.assign(this.controls, { enableDamping: true, dampingFactor: 0.08, rotateSpeed: 0.6, minDistance: 2.5, maxDistance: 60, maxPolarAngle: 1.48 });

    this.scene.background = BG.clone();
    this.scene.fog = new THREE.Fog(BG.clone(), 20, 78);
    this.scene.add(buildTerrain(), buildOrigin());
    // Soft hemispheric light (sky ink, ground bg) plus a dim key from above-left: matte volumes.
    const key = new THREE.DirectionalLight(0xffffff, 0.9); key.position.set(-4, 9, 5);
    this.scene.add(new THREE.HemisphereLight(0xffffff, BG.clone(), 2.2), key);

    const resize = () => {
      const w = container.clientWidth, h = container.clientHeight;
      this.renderer.setSize(w, h); this.labels.setSize(w, h);
      this.camera.aspect = w / Math.max(h, 1);
      // Keep the twin framed on narrow, tall stages: widen the vertical fov.
      this.camera.fov = this.camera.aspect < 1 ? 40 + 22 * (1 - this.camera.aspect) : 40;
      this.camera.updateProjectionMatrix();
    };
    new ResizeObserver(resize).observe(container); resize();
    this.bindPointer();
    requestAnimationFrame(this.loop);
  }

  /** Hover (mouse) shows one label; a tap (touch or click without drag) pins it until the next tap. */
  private bindPointer() {
    const el = this.renderer.domElement;
    let down: { x: number; y: number; t: number } | null = null;
    const local = (e: PointerEvent) => { const r = el.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    el.addEventListener('pointermove', e => { if (e.pointerType === 'mouse') this.pointer = local(e); });
    el.addEventListener('pointerleave', () => { this.pointer = null; });
    el.addEventListener('pointerdown', e => { down = { ...local(e), t: performance.now() }; });
    el.addEventListener('pointerup', e => {
      const p = local(e);
      if (down && Math.hypot(p.x - down.x, p.y - down.y) < 6 && performance.now() - down.t < 400) this.pinned = this.pick(p);
      down = null;
    });
  }

  /** Nearest entity or device frustum to a screen point, within 28 px. */
  private pick(p: { x: number; y: number }): object | null {
    const w = this.container.clientWidth, h = this.container.clientHeight, v = new THREE.Vector3();
    const dist = (o: THREE.Object3D, dy = 0) => {
      v.setFromMatrixPosition(o.matrixWorld); v.y += dy; v.project(this.camera);
      if (v.z < -1 || v.z > 1) return Infinity;
      return Math.hypot((v.x * 0.5 + 0.5) * w - p.x, (-v.y * 0.5 + 0.5) * h - p.y);
    };
    let best: object | null = null, bd = 28;
    for (const e of this.entities.values()) {
      if (!e.group.visible || e.a.solid + e.a.outline < 0.05) continue;
      const half = kinds()[e.kind].half;
      const d = Math.min(dist(e.body, -half * 0.8), dist(e.body), dist(e.body, half * 0.8));
      if (d < bd) { bd = d; best = e; }
    }
    for (const f of this.frustums.values()) if (f.group.visible) { const d = dist(f.group); if (d < bd) { bd = d; best = f; } }
    return best;
  }

  /** Stable line style per device for its lifetime in this view (0 solid, 1 dashed, 2 dotted). */
  styleOf(key: string): number {
    let s = this.styles.get(key);
    if (s === undefined) { s = this.styles.size % LINE_STYLES.length; this.styles.set(key, s); }
    return s;
  }

  // ---- per-frame: fades and decay ----
  private loop = (now: number) => {
    const dt = Math.min(0.1, Math.max(0, (now - this.lastT) / 1000)); this.lastT = now;
    const k = 1 - Math.exp(-dt / 0.15); // ~450 ms to settle
    this.controls.update();
    this.camera.updateMatrixWorld(); this.scene.updateMatrixWorld();
    this.hovered = this.pointer ? this.pick(this.pointer) : null;
    if (this.pinned && !this.isLive(this.pinned)) this.pinned = null;
    const focus = this.hovered ?? this.pinned;

    for (const e of [...this.entities.values(), ...this.dying]) {
      const t = targets(e, now);
      for (const c of CHANNELS) e.a[c] += (t[c] - e.a[c]) * k;
      const set = (o: THREE.Object3D & { material: THREE.Material | THREE.Material[] }, v: number) => {
        (o.material as THREE.Material).opacity = v; o.visible = v > 0.004;
      };
      set(e.solid, e.a.solid); set(e.outline, e.a.outline);
      (e.solid.material as THREE.MeshLambertMaterial).color.copy(INK).lerp(INK3, e.a.tone);
      // Opaque while fully present (correct depth), blended only while fading.
      (e.solid.material as THREE.Material).transparent = e.a.solid < 0.94;
      set(e.arrow, e.a.arrow); set(e.drop, e.a.drop); set(e.trail, e.a.trail);
      const lo = e === focus && !e.dying ? 1 : 0;
      if (lo !== e.labelOpacity) { e.labelOpacity = lo; e.label.visible = lo > 0; }
    }
    this.dying = this.dying.filter(e => {
      if (CHANNELS.some(c => c !== 'tone' && e.a[c] > 0.004)) return true;
      this.dispose(e); return false;
    });
    // Only a lost device blinks.
    const blink = blinkAt(now);
    for (const f of this.frustums.values()) {
      (f.line.material as THREE.Material).opacity = f.silent ? 0.8 * blink : 0.7;
      f.label.visible = f === focus || f.silent;
    }
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
    requestAnimationFrame(this.loop);
  };

  private isLive(o: object): boolean {
    for (const e of this.entities.values()) if (e === o) return true;
    for (const f of this.frustums.values()) if (f === o) return f.group.visible;
    return false;
  }

  private makeEntity(g: GlobalEntity): Ent {
    const kind = kindOf(g.class), K = kinds()[kind];
    const group = new THREE.Group(), body = new THREE.Group();
    const solid = new THREE.Mesh(K.solid, new THREE.MeshLambertMaterial({ color: INK, transparent: true, opacity: 0, fog: false }));
    const outline = new THREE.LineSegments(K.outline, dottedMat(0));
    const ag = new THREE.BufferGeometry(); ag.setAttribute('position', new THREE.Float32BufferAttribute(new Float32Array(18), 3));
    const arrow = new THREE.LineSegments(ag, lineMat(0, 0));
    body.add(solid, outline, arrow);
    const dg = new THREE.BufferGeometry(); dg.setAttribute('position', new THREE.Float32BufferAttribute(new Float32Array(6), 3));
    const drop = new THREE.Line(dg, dottedMat(0));
    const tg = new THREE.BufferGeometry();
    tg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(TRAIL_MAX * 3), 3));
    tg.setAttribute('color', new THREE.BufferAttribute(new Float32Array(TRAIL_MAX * 3), 3));
    tg.setDrawRange(0, 0);
    const trail = new THREE.Line(tg, lineMat(0, 0, true));
    const el = document.createElement('div'); el.className = 'tag3d';
    const label = new CSS2DObject(el); label.center.set(0, 1); label.position.set(0.06, K.half + 0.12, 0); label.visible = false;
    body.add(label);
    group.add(body, drop, trail);
    this.scene.add(group);
    return {
      gid: g.gid, cls: g.class, kind, group, body, solid, outline, arrow, drop, trail, trailStyle: 0,
      label, labelText: '', labelOpacity: 0, pts: [], staleSince: null, dying: false,
      a: { solid: 0, tone: 0, outline: 0, arrow: 0, drop: 0, trail: 0 },
    };
  }

  private retire(e: Ent) { this.entities.delete(e.gid); e.dying = true; this.dying.push(e); }

  private dispose(e: Ent) {
    e.label.removeFromParent(); // CSS2DObject removes its element on 'removed'
    this.scene.remove(e.group);
    for (const o of [e.solid, e.outline, e.arrow, e.drop, e.trail]) (o.material as THREE.Material).dispose();
    e.arrow.geometry.dispose(); e.drop.geometry.dispose(); e.trail.geometry.dispose();
  }

  private updateEntity(e: Ent, g: GlobalEntity, style: number, now: number) {
    const K = kinds()[e.kind];
    if (g.stale) e.staleSince ??= now; else e.staleSince = null;
    const [x, y0, z] = g.pos;
    e.body.position.set(x, y0 + K.half, z);
    const dp = e.drop.geometry.getAttribute('position') as THREE.BufferAttribute;
    dp.setXYZ(0, x, 0.012, z); dp.setXYZ(1, x, y0, z); dp.needsUpdate = true; e.drop.computeLineDistances();
    e.drop.geometry.setDrawRange(0, y0 > 0.05 ? 2 : 0);
    e.drop.geometry.computeBoundingSphere();

    // Velocity: a 1 px shaft with a chevron head, only when the entity is actually moving.
    const ap = e.arrow.geometry.getAttribute('position') as THREE.BufferAttribute;
    const v = new THREE.Vector3(...g.vel), len = v.length();
    if (len > ARROW_MIN) {
      const L = Math.min(len, 2), d = v.clone().divideScalar(len), tip = d.clone().multiplyScalar(L);
      const side = new THREE.Vector3(-d.z, 0, d.x); if (side.lengthSq() < 1e-6) side.set(1, 0, 0); side.normalize();
      const h = Math.min(0.14, L * 0.4), back = tip.clone().addScaledVector(d, -h);
      const w1 = back.clone().addScaledVector(side, h * 0.55), w2 = back.clone().addScaledVector(side, -h * 0.55);
      [0, 0, 0, tip.x, tip.y, tip.z, tip.x, tip.y, tip.z, w1.x, w1.y, w1.z, tip.x, tip.y, tip.z, w2.x, w2.y, w2.z].forEach((c, i) => ((ap.array as Float32Array)[i] = c));
      ap.needsUpdate = true; e.arrow.geometry.setDrawRange(0, 6);
    } else e.arrow.geometry.setDrawRange(0, 0);
    e.arrow.geometry.computeBoundingSphere();

    // Trail: the last 3 s, in the line style of the (first) source device, fading towards its tail.
    if (style !== e.trailStyle) { (e.trail.material as THREE.Material).dispose(); e.trail.material = lineMat(style, e.a.trail, true); e.trailStyle = style; }
    e.pts.push({ p: new THREE.Vector3(x, y0 + 0.02, z), t: now });
    while (e.pts.length > TRAIL_MAX || (e.pts.length && now - e.pts[0].t > TRAIL_MS)) e.pts.shift();
    const tp = e.trail.geometry.getAttribute('position') as THREE.BufferAttribute, tc = e.trail.geometry.getAttribute('color') as THREE.BufferAttribute;
    const n = e.pts.length;
    for (let i = 0; i < n; i++) { const { p, t } = e.pts[i], f = Math.pow(1 - (now - t) / TRAIL_MS, 1.5); tp.setXYZ(i, p.x, p.y, p.z); tc.setXYZ(i, f, f, f); }
    tp.needsUpdate = true; tc.needsUpdate = true; e.trail.geometry.setDrawRange(0, n);
    e.trail.geometry.computeBoundingSphere();
    if (style) e.trail.computeLineDistances();

    const state = e.staleSince === null ? (len > ARROW_MIN ? `${len.toFixed(1)} m/s` : 'still') : `stale ${((now - e.staleSince) / 1000).toFixed(0)} s`;
    const text = `${className(g.class)} · ${g.gid} · ${state}`;
    if (text !== e.labelText) { e.labelText = text; e.label.element.textContent = text; }
  }

  update(snap: Snapshot) {
    const now = performance.now();
    const keyOfId = new Map<number, string>();
    for (const d of snap.devices) { const dk = d.key ?? String(d.deviceId); keyOfId.set(d.deviceId, dk); this.styleOf(dk); }
    const styleOfSource = (g: GlobalEntity) => { const s = g.sources[0]; return s ? this.styleOf(keyOfId.get(s.deviceId) ?? `id:${s.deviceId}`) : 0; };

    const seen = new Set<string>();
    // Global (fused) entities are what the operator sees; per-device views become ghosts.
    for (const g of snap.global) {
      seen.add(g.gid);
      let e = this.entities.get(g.gid);
      if (e && e.cls !== g.class) { this.retire(e); e = undefined; } // re-classified, or gid reused after a server restart
      if (!e) { e = this.makeEntity(g); this.entities.set(g.gid, e); }
      this.updateEntity(e, g, styleOfSource(g), now);
    }
    for (const [gid, e] of this.entities) if (!seen.has(gid)) this.retire(e);

    // Ghosts: each device's raw view as a small dotted sphere above the body, on a stem in that device's line style.
    const seenGhosts = new Set<string>();
    if (this.showGhosts) for (const d of snap.devices) for (const en of d.entities) {
      const dk = d.key ?? String(d.deviceId), style = this.styleOf(dk);
      const k = `${dk}#${en.id}`; seenGhosts.add(k);
      let gh = this.ghosts.get(k);
      if (!gh) {
        const group = new THREE.Group();
        const sphere = new THREE.LineSegments(this.ghostSphere, dottedMat(0.9));
        const sg = new THREE.BufferGeometry(); sg.setAttribute('position', new THREE.Float32BufferAttribute([0, 0, 0, 0, -1, 0], 3));
        const stem = new THREE.Line(sg, lineMat(style, 0.4));
        group.add(sphere, stem); this.scene.add(group);
        gh = { group, sphere, stem, style }; this.ghosts.set(k, gh);
      }
      // Float just above the body (bodies are opaque now), stem down to the ground.
      const h = en.pos[1] + 2 * kinds()[kindOf(en.class)].half + 0.22;
      gh.group.position.set(en.pos[0], h, en.pos[2]);
      const sp = gh.stem.geometry.getAttribute('position') as THREE.BufferAttribute;
      sp.setY(0, -0.09); sp.setY(1, -h + 0.012); sp.needsUpdate = true; gh.stem.computeLineDistances(); gh.stem.geometry.computeBoundingSphere();
      (gh.sphere.material as THREE.Material).opacity = en.stale ? 0.3 : 0.85;
    }
    for (const [k, gh] of this.ghosts) if (!seenGhosts.has(k)) {
      this.scene.remove(gh.group); (gh.sphere.material as THREE.Material).dispose(); (gh.stem.material as THREE.Material).dispose(); gh.stem.geometry.dispose();
      this.ghosts.delete(k);
    }

    // Camera frustums, one per device pose, in the device's line style.
    const seenDev = new Set<string>();
    for (const d of snap.devices) {
      const dk = d.key ?? String(d.deviceId);
      seenDev.add(dk);
      let f = this.frustums.get(dk);
      if (!f) {
        const style = this.styleOf(dk), group = new THREE.Group();
        const line = new THREE.LineSegments(this.frustumGeometry, lineMat(style, 0.7));
        const el = document.createElement('div'); el.className = 'tag3d';
        const label = new CSS2DObject(el); label.center.set(-0.1, 1.2); label.visible = false;
        group.add(line, label);
        this.scene.add(group); f = { group, line, label, style, silent: false }; this.frustums.set(dk, f);
      }
      f.silent = d.silent;
      const text = `${d.provisional ? `dev ? · ${d.addr}` : `dev ${d.deviceId}`}${d.silent ? ' · lost' : ''}`;
      if (f.label.element.textContent !== text) f.label.element.textContent = text;
      f.label.element.classList.toggle('lost', d.silent);
      if (d.pose) { f.group.position.set(...d.pose.pos); f.group.quaternion.set(...d.pose.quat); f.group.visible = true; } else f.group.visible = false;
    }
    for (const [k, f] of this.frustums) if (!seenDev.has(k)) {
      f.label.removeFromParent();
      this.scene.remove(f.group); (f.line.material as THREE.Material).dispose();
      this.frustums.delete(k);
    }
  }
}
