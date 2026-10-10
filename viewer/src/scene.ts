// The world twin (docs/STYLE.md). Wire is the environment: one procedural contour field at --ink-4,
// device frustums, trails and axes as 1 px lines (device = line style). Fill is what the twin
// believes exists: entities are matte monochrome solids under soft hemispheric light (class =
// silhouette: dismount capsule, carried sphere, static box, vehicle a low car-sized box turned to its
// heading, two-wheeler a smaller one). Trust decays in steps: live = bright
// solid -> coasting (the device missed its heartbeat) = the same solid at --ink-2 -> stale = fading
// toward --ink-3 -> dotted outline -> gone. Each entity stands on a hairline ground ring whose
// radius is its honest error `ce` (V2): theta while the heartbeat holds, widening at the class max
// speed while coasting, snapping back on the next keyframe. Restraint: entities are the brightest
// thing on screen, trails last 3 s, labels appear only on hover or tap, the terrain never moves on
// its own, and only a lost device blinks.
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { CSS2DObject, CSS2DRenderer } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { fmtRadius } from './link';
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

type Kind = 'person' | 'carried' | 'static' | 'vehicle' | 'cycle';
const KINDS: Kind[] = ['person', 'carried', 'static', 'vehicle', 'cycle'];
// COCO ids. Person = capsule, things people carry = small sphere, road vehicles (car, bus, truck) = a
// low car-sized box, two-wheelers (bicycle, motorcycle) = a smaller one, everything else = box.
// Class 0 is shown as "dismount" (the military term); the wire and the code keep COCO's person.
// From 100 up, MinBand's own ids (core/src/classes.rs): 100 an unclassified ground mover from motion
// detection, drawn as the small heading box because its size is unknown; 101 armoured, a vehicle.
const CARRIED = new Set([24, 25, 26, 27, 28, 39, 40, 41, 42, 43, 44, 64, 65, 67, 73, 76, 79]);
const VEHICLES = new Set([2, 5, 7, 101]), CYCLES = new Set([1, 3, 100]);
const kindOf = (cls: number): Kind => cls === 0 ? 'person' : VEHICLES.has(cls) ? 'vehicle' : CYCLES.has(cls) ? 'cycle' : CARRIED.has(cls) ? 'carried' : 'static';
const CLASS_NAME: Record<number, string> = {
  0: 'dismount', 1: 'bicycle', 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck',
  24: 'backpack', 25: 'umbrella', 26: 'handbag', 28: 'suitcase', 39: 'bottle', 41: 'cup',
  56: 'chair', 57: 'couch', 58: 'plant', 59: 'bed', 60: 'table', 62: 'tv', 63: 'laptop', 67: 'phone', 73: 'book',
  100: 'mover', 101: 'armoured',
};
const className = (cls: number) => CLASS_NAME[cls] ?? `class ${cls}`;

// ---- materials -------------------------------------------------------------------------------
const additive = { transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, fog: false } as const;
function lineMat(style = 0, opacity = 1, vertexColors = false): THREE.LineBasicMaterial {
  const s = LINE_STYLES[style % LINE_STYLES.length];
  const p = { ...additive, color: INK, opacity, vertexColors };
  return s.dash ? new THREE.LineDashedMaterial({ ...p, dashSize: s.dash, gapSize: s.gap }) : new THREE.LineBasicMaterial(p);
}
/** Dotted pattern (m): stale outlines, drop lines. */
const DOT = { dash: 0.018, gap: 0.045 };
const dottedMat = (opacity = 1) => new THREE.LineDashedMaterial({ ...additive, color: INK, opacity, dashSize: DOT.dash, gapSize: DOT.gap });

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
interface KindGeo {
  solid: THREE.BufferGeometry; outline: THREE.BufferGeometry; half: number;
  /** Horizontal half-extent along the heading (m): where a velocity arrow starts in wide mode. */
  reach: number;
  /** Wide mode: the glyph's longest dimension (m) is drawn at least `minPx` CSS px long, like a map symbol. */
  size: number; minPx: number;
  /** Turns to its heading while it moves (vehicles; the long axis is local x). */
  heads: boolean;
}
/** Box glyph, length (x) by height (y) by width (z), metres. */
const box = (x: number, y: number, z: number) => ({ solid: new THREE.BoxGeometry(x, y, z), outline: dashed(new THREE.EdgesGeometry(new THREE.BoxGeometry(x, y, z))), half: y / 2 });
let KIND: Record<Kind, KindGeo> | null = null;
function kinds(): Record<Kind, KindGeo> {
  if (KIND) return KIND;
  KIND = {
    person: { solid: new THREE.CapsuleGeometry(0.2, 1.3, 8, 24), outline: dashed(revolve(capsuleProfile(0.2, 1.3), [0.12, 0.5, 0.88], 4, 28, 36, Math.PI / 4)), half: 0.85, reach: 0.2, size: 1.7, minPx: 26, heads: false },
    carried: { solid: new THREE.SphereGeometry(0.14, 28, 18), outline: dashed(revolve(capsuleProfile(0.14, 0), [0.5], 3, 20, 28)), half: 0.14, reach: 0.14, size: 0.28, minPx: 8, heads: false },
    static: { ...box(0.4, 0.4, 0.4), reach: 0.2, size: 0.4, minPx: 9, heads: false },
    // Car-sized for car, bus and truck alike (the tag names which); bicycle and motorcycle smaller.
    vehicle: { ...box(4.4, 1.5, 1.8), reach: 2.2, size: 4.4, minPx: 24, heads: true },
    cycle: { ...box(1.9, 1.1, 0.6), reach: 0.95, size: 1.9, minPx: 15, heads: true },
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

/** Height of the drawn contour field, for things that lie on it (rings). Zero in the flat basin. */
const groundAt = (x: number, z: number) => Math.hypot(x, z) < 6 ? 0 : terrainHeight(x, z) - 0.004;

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

// ---- wide area: real drone footage, entities over 50-200 m ---------------------------------------
// The contour field is drawn for a room around the marker (104 m, a flat 6 m basin, swells and a
// ridge entities would sink into). When the entities' extent is far beyond that, the scene switches
// to a wide mode: a flat hairline grid scaled to the extent, fog, far plane and zoom limit scaled
// with it, the camera framed once on the entities, and glyphs magnified like map symbols so they
// stay legible at hundreds of metres. Ring radii stay true metres: they are error bars.
/** Room view: the camera, fog, far plane and zoom limit the room mode has always had. */
const ROOM_POS = new THREE.Vector3(7.4, 3.9, 9.2), ROOM_TARGET = new THREE.Vector3(0, 0.7, 0);
const ROOM_FOG = [20, 78] as const, ROOM_FAR = 300, ROOM_MAX_DIST = 60;
/** Entered when the 90th-percentile distance of the entities from their centroid stays above
 * WIDE_ENTER_M for WIDE_ENTER_MS, left when it stays below WIDE_EXIT_M for WIDE_EXIT_MS. */
const WIDE_ENTER_M = 15, WIDE_EXIT_M = 8, WIDE_ENTER_MS = 400, WIDE_EXIT_MS = 2500;
/** Scale radii (m): grid, fog, far plane and zoom limit follow the entities' extent in these steps. */
const WIDE_STEPS = [20, 50, 100, 200, 500, 1000, 2000, 5000];
/** Framing: elevation above the horizon, share of the view the bounds fill, glide time. For
 * WIDE_SETTLE_MS after the mode starts the framing follows bounds that are still growing (a feed
 * fills in over its first second), unless the operator has taken the orbit. */
const WIDE_ELEV = (55 * Math.PI) / 180, FRAME_FILL = 0.84, GLIDE_MS = 600, WIDE_SETTLE_MS = 2500;
/** Screen px per metre at the room view's distance: dash patterns keep that on-screen length in wide mode. */
const ROOM_PX_PER_M = 90;
/** Wide mode: depth of a device frustum on screen (px), arms of its ground cross (px). */
const FRUSTUM_PX = 44, NADIR_PX = 6;

/** Wide-mode ground: a flat grid of `cell` metres, `half` metres around (cx, cz), at --ink-4 fading out radially. */
function buildGrid(cx: number, cz: number, half: number, cell: number): THREE.LineSegments {
  const n = Math.round(half / cell), p: number[] = [], c: number[] = [], col = new THREE.Color();
  const fade = (x: number, z: number) => 1 - smooth(0.5 * half, half, Math.hypot(x - cx, z - cz));
  const seg = (x0: number, z0: number, x1: number, z1: number) => {
    const f0 = fade(x0, z0), f1 = fade(x1, z1);
    if (f0 <= 0 && f1 <= 0) return;
    for (const [x, z, f] of [[x0, z0, f0], [x1, z1, f1]]) { p.push(x, 0, z); col.copy(BG).lerp(INK4, f); c.push(col.r, col.g, col.b); }
  };
  for (let i = -n; i <= n; i++) for (let j = -n; j < n; j++) {
    seg(cx + j * cell, cz + i * cell, cx + (j + 1) * cell, cz + i * cell);
    seg(cx + i * cell, cz + j * cell, cx + i * cell, cz + (j + 1) * cell);
  }
  const g = segs(p); g.setAttribute('color', new THREE.Float32BufferAttribute(c, 3));
  // Below everything: it never hides a ring or a trail lying on it.
  return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true, fog: true, depthWrite: false }));
}
/** Dash pattern of a line material, scaled (wide mode keeps dashes at their room length on screen). */
function setDash(m: THREE.Material, dash: number, gap: number) {
  if (m instanceof THREE.LineDashedMaterial) { m.dashSize = dash; m.gapSize = gap; }
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
/** Ring line style by trust: live solid, coasting dashed, stale dotted (never a device's style). */
type RingStyle = 'live' | 'coasting' | 'stale';
interface Ent {
  gid: string; cls: number; kind: Kind;
  /** `body` is the glyph's centre (arrow and tag hang off it); `glyph` holds the solid and outline, turned to `yaw`. */
  group: THREE.Group; body: THREE.Group; glyph: THREE.Group; yaw: number;
  /** Ground height under the glyph (pos y) and the glyph's magnification (wide mode; 1 in room mode). */
  y0: number; scale: number;
  solid: THREE.Mesh; outline: THREE.LineSegments;
  arrow: THREE.LineSegments; drop: THREE.Line; trail: THREE.Line; trailStyle: number;
  ring: THREE.Line; ringTicks: THREE.LineSegments;
  /** Displayed radius (eases up, snaps down), the server's `ce` it chases (null: no ring), and the last geometry built. */
  ringR: number; ringTarget: number | null; ringStyle: RingStyle; ringKey: string;
  label: CSS2DObject; labelText: string; labelOpacity: number;
  pts: { p: THREE.Vector3; t: number }[];
  coasting: boolean; coastSince: number | null;
  staleSince: number | null; dying: boolean;
  a: Record<Channel, number>;
}
// `tone` is 0 for --ink (fresh) .. 1 for --ink-3 (about to lose its body).
type Channel = 'solid' | 'tone' | 'outline' | 'arrow' | 'drop' | 'trail' | 'ring';
const CHANNELS: Channel[] = ['solid', 'tone', 'outline', 'arrow', 'drop', 'trail', 'ring'];
const TRAIL_MS = 3000, TRAIL_MAX = 120;
const ARROW_MIN = 0.3; // m/s
/** Vehicles turn to their velocity above this speed (m/s); slower, a parked car's jitter would spin it. */
const HEADING_MIN = 1;
// Decay schedule, seconds since the entity went stale: solid fading to --ink-3, then dotted outline, then gone.
const SOLID_S = 3, OUTLINE_S = 8;
/** Coasting tone: the solid at about --ink-2, between live and the stale fade. */
const COAST_TONE = 0.4;

// V2 rings. Up to RING_FULL_R a ring is drawn at its full channel opacity; beyond, it fades as
// sqrt(RING_FULL_R / r) down to RING_FLOOR, so a dozen blackout rings stay quieter than the solids.
// Past RING_MAX_R (the flat basin and the first swells) the ring stops growing on screen and four
// short outward ticks say it is larger; the tag gives the real radius. Rings lie on the contour
// field, so a wide one climbs the swells instead of cutting through them.
const RING_N = 128, RING_FULL_R = 1.5, RING_FLOOR = 0.3, RING_MAX_R = 12, RING_TICK = 0.7;

function targets(e: Ent, now: number): Record<Channel, number> {
  const zero = { solid: 0, tone: 1, outline: 0, arrow: 0, drop: 0, trail: 0, ring: 0 };
  if (e.dying) return zero;
  if (e.staleSince === null) {
    // Coasting: dimmer, the velocity is a guess now (arrow down), the ring carries the message.
    if (e.coasting) return { solid: 0.92, tone: COAST_TONE, outline: 0, arrow: 0.25, drop: 0.4, trail: 0.35, ring: 1 };
    return { solid: 0.95, tone: 0, outline: 0, arrow: 0.7, drop: 0.5, trail: 0.6, ring: 0.5 };
  }
  const s = (now - e.staleSince) / 1000;
  if (s < SOLID_S) return { solid: 0.88, tone: COAST_TONE + (1 - COAST_TONE) * s / SOLID_S, outline: 0, arrow: 0, drop: 0.3, trail: 0.3, ring: 0.7 };
  if (s < OUTLINE_S) return { solid: 0, tone: 1, outline: 0.75, arrow: 0, drop: 0.2, trail: 0.12, ring: 0.45 };
  return zero;
}

interface Frustum {
  group: THREE.Group; line: THREE.LineSegments; label: CSS2DObject; style: number; silent: boolean;
  /** Wide mode: a dotted drop from the device to the ground and a small cross where it lands (its nadir). */
  ground: THREE.Group; nadir: THREE.Line; foot: THREE.LineSegments;
  /** Magnification of the frustum (wide mode; 1 in room mode), eased. */
  scale: number;
}
interface View { pos: THREE.Vector3; target: THREE.Vector3 }

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

  // ---- wide mode ----
  private terrain: THREE.LineSegments;
  private grid: THREE.LineSegments | null = null;
  private gridAt = { x: 0, z: 0, r: 0 };
  private wideOn = false;
  /** Current scale radius (one of WIDE_STEPS), 0 in room mode. */
  private wideR = 0;
  /** Since when the extent has said "switch" (hysteresis), and whether any extent was measured yet. */
  private flipSince: number | null = null;
  private measured = false;
  private bounds = { minX: 0, maxX: 0, minZ: 0, maxZ: 0 };
  /** Ground positions of the entities at the last measure (x, z pairs): what a framing fits. */
  private spots: number[] = [];
  /** The bounds and bearing the view was last framed on, and until when the start-up framing may follow them. */
  private framed = { minX: 0, maxX: 0, minZ: 0, maxZ: 0 };
  private framedBearing = 0;
  private settleUntil = 0;
  /** The room view to return to, saved when the wide mode starts. */
  private roomView: View | null = null;
  private glide: { t0: number; from: View; to: View; done?: () => void } | null = null;
  /** Glyph magnification per kind and the dash scale: 1 in room mode, eased (`easeK` this frame). */
  private sigma = Object.fromEntries(KINDS.map(k => [k, 1])) as Record<Kind, number>;
  private dashQ = 1;
  private easeK = 1;
  /** Screen px per metre at the orbit target. */
  private pxPerM = ROOM_PX_PER_M;
  private emittedCell = 0;
  /** Called when the wide mode starts or ends, or its grid spacing changes (`cellM` 0 in room mode). */
  onWide: ((wide: boolean, cellM: number) => void) | null = null;
  /** Wide-area mode: the entities spread far beyond room scale (real drone footage). */
  get wide() { return this.wideOn; }

  constructor(private container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    container.appendChild(this.renderer.domElement);
    Object.assign(this.labels.domElement.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
    container.appendChild(this.labels.domElement);

    this.camera = new THREE.PerspectiveCamera(40, 1, 0.1, ROOM_FAR);
    this.camera.position.copy(ROOM_POS);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.copy(ROOM_TARGET);
    Object.assign(this.controls, { enableDamping: true, dampingFactor: 0.08, rotateSpeed: 0.6, minDistance: 2.5, maxDistance: ROOM_MAX_DIST, maxPolarAngle: 1.48 });
    // The operator's orbit always wins over a framing glide, and ends the start-up framing.
    this.controls.addEventListener('start', () => { this.settleUntil = 0; this.endGlide(); });

    this.scene.background = BG.clone();
    this.scene.fog = new THREE.Fog(BG.clone(), ...ROOM_FOG);
    this.terrain = buildTerrain();
    this.scene.add(this.terrain, buildOrigin());
    // Soft hemispheric light (sky ink, ground bg) plus a dim key from above-left: matte volumes.
    const key = new THREE.DirectionalLight(0xffffff, 0.9); key.position.set(-4, 9, 5);
    this.scene.add(new THREE.HemisphereLight(0xffffff, BG.clone(), 2.2), key);

    const resize = () => {
      const w = container.clientWidth, h = container.clientHeight;
      this.renderer.setSize(w, h); this.labels.setSize(w, h);
      this.camera.aspect = w / Math.max(h, 1);
      // Keep the twin framed on narrow, tall stages (phones, STAGE beside the panels): widen the
      // vertical fov until the horizontal one holds about 44 degrees, up to 58.
      const a = this.camera.aspect, fit = (2 * Math.atan(Math.tan((22 * Math.PI) / 180) / Math.max(a, 0.1)) * 180) / Math.PI;
      this.camera.fov = Math.max(a < 1 ? 40 + 22 * (1 - a) : 40, Math.min(58, fit));
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
      const half = kinds()[e.kind].half * e.scale;
      const d = Math.min(dist(e.body, -half * 0.8), dist(e.body), dist(e.body, half * 0.8));
      if (d < bd) { bd = d; best = e; }
    }
    for (const f of this.frustums.values()) if (f.group.visible) { const d = dist(f.group); if (d < bd) { bd = d; best = f; } }
    return best;
  }

  /** Where an entity's body is on screen (CSS px in the scene container), or null. For e2e tests. */
  screenOf(gid: string): { x: number; y: number } | null {
    const e = this.entities.get(gid);
    if (!e) return null;
    const v = new THREE.Vector3().setFromMatrixPosition(e.body.matrixWorld).project(this.camera);
    return { x: (v.x * 0.5 + 0.5) * this.container.clientWidth, y: (-v.y * 0.5 + 0.5) * this.container.clientHeight };
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
    this.stepGlide(now);
    this.controls.update();
    this.scaleSymbols(dt);
    this.camera.updateMatrixWorld(); this.scene.updateMatrixWorld();
    this.hovered = this.pointer ? this.pick(this.pointer) : null;
    if (this.pinned && !this.isLive(this.pinned)) this.pinned = null;
    const focus = this.hovered ?? this.pinned;
    const q = this.dashQ;

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
      const yaw = e.glyph.rotation.y;
      if (yaw !== e.yaw) e.glyph.rotation.y = Math.abs(e.yaw - yaw) < 1e-3 ? e.yaw : yaw + (e.yaw - yaw) * k;
      // Wide mode: the glyph magnified like a map symbol, standing on the ground; dashes keep their screen length.
      const s = this.sigma[e.kind];
      if (s !== e.scale) {
        const half = kinds()[e.kind].half;
        e.scale = s; e.glyph.scale.setScalar(s);
        e.body.position.y = e.y0 + half * s; e.label.position.set(0.06 * s, (half + 0.12) * s, 0);
      }
      setDash(e.outline.material as THREE.Material, (DOT.dash * q) / s, (DOT.gap * q) / s);
      setDash(e.drop.material as THREE.Material, DOT.dash * q, DOT.gap * q);
      const ts = LINE_STYLES[e.trailStyle % LINE_STYLES.length];
      if (ts.dash) setDash(e.trail.material as THREE.Material, ts.dash * q, ts.gap * q);
      this.updateRing(e, dt);
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
      // Wide mode: the frustum drawn at a legible size at its own distance (a device symbol), its nadir dropped to the ground.
      const sf = f.scale = this.ease(f.scale, this.wideOn ? Math.max(1, FRUSTUM_PX / (1.2 * this.pxAt(f.group.position))) : 1);
      const st = LINE_STYLES[f.style % LINE_STYLES.length];
      if (f.line.scale.x !== sf) f.line.scale.setScalar(sf);
      if (st.dash) setDash(f.line.material as THREE.Material, (st.dash * q) / sf, (st.gap * q) / sf);
      f.ground.visible = this.wideOn && f.group.visible;
      if (f.ground.visible) {
        (f.nadir.material as THREE.Material).opacity = 0.4 * (f.silent ? blink : 1);
        (f.foot.material as THREE.Material).opacity = 0.55 * (f.silent ? blink : 1);
        setDash(f.nadir.material as THREE.Material, DOT.dash * q, DOT.gap * q * 1.5);
        f.foot.scale.setScalar(NADIR_PX / this.pxPerM);
      }
    }
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
    requestAnimationFrame(this.loop);
  };

  /** V2: radius from `ce`, line style from trust, opacity from the channel and the radius. */
  private updateRing(e: Ent, dt: number) {
    const target = e.ringTarget;
    if (target === null) { e.ring.visible = false; e.ringTicks.visible = false; return; }
    // A refresh resets the error, so shrinking snaps; growth eases (~0.2 s) so a jump in ce reads as growth.
    if (e.ringR < 0 || target < e.ringR) e.ringR = target; else e.ringR += (target - e.ringR) * (1 - Math.exp(-dt / 0.12));
    // Wide mode: flat ground, and the cap is half the scale radius instead of the room's basin.
    const wide = this.wideOn, maxR = wide ? Math.max(RING_MAX_R, this.wideR / 2) : RING_MAX_R;
    const ground = wide ? () => 0 : groundAt, tick = wide ? 8 / this.pxPerM : RING_TICK;
    const r = Math.min(e.ringR, maxR), clipped = e.ringR > maxR;
    const op = e.a.ring * (r <= RING_FULL_R ? 1 : Math.max(RING_FLOOR, Math.sqrt(RING_FULL_R / r)));
    const rm = e.ring.material as THREE.LineDashedMaterial, tm = e.ringTicks.material as THREE.Material;
    rm.opacity = op; e.ring.visible = op > 0.004 && r > 0.01;
    tm.opacity = op; e.ringTicks.visible = e.ring.visible && clipped;
    if (!e.ring.visible) return;
    const cx = e.body.position.x, cz = e.body.position.z;
    const key = `${cx.toFixed(3)} ${cz.toFixed(3)} ${r.toFixed(3)} ${e.ringStyle}${wide ? ` wide ${clipped ? tick.toFixed(1) : ''}` : ''}`;
    if (key === e.ringKey) return;
    e.ringKey = key;
    const p = e.ring.geometry.getAttribute('position') as THREE.BufferAttribute;
    for (let i = 0; i <= RING_N; i++) {
      const a = (i / RING_N) * Math.PI * 2, x = cx + r * Math.cos(a), z = cz + r * Math.sin(a);
      p.setXYZ(i, x, ground(x, z) + 0.02, z);
    }
    p.needsUpdate = true; e.ring.computeLineDistances();
    // A fixed number of dashes or dots around the ring at any size.
    const c = 2 * Math.PI * r;
    if (e.ringStyle === 'live') { rm.dashSize = c; rm.gapSize = 0; }
    else if (e.ringStyle === 'coasting') { rm.dashSize = 0.55 * c / 48; rm.gapSize = 0.45 * c / 48; }
    else { rm.dashSize = 0.15 * c / 96; rm.gapSize = 0.85 * c / 96; }
    if (clipped) {
      const t = e.ringTicks.geometry.getAttribute('position') as THREE.BufferAttribute;
      for (let i = 0; i < 4; i++) {
        const a = (i / 4) * Math.PI * 2 + Math.PI / 4, ca = Math.cos(a), sa = Math.sin(a);
        const x0 = cx + r * ca, z0 = cz + r * sa, x1 = cx + (r + tick) * ca, z1 = cz + (r + tick) * sa;
        t.setXYZ(2 * i, x0, ground(x0, z0) + 0.02, z0); t.setXYZ(2 * i + 1, x1, ground(x1, z1) + 0.02, z1);
      }
      t.needsUpdate = true;
    }
  }

  private isLive(o: object): boolean {
    for (const e of this.entities.values()) if (e === o) return true;
    for (const f of this.frustums.values()) if (f === o) return f.group.visible;
    return false;
  }

  private makeEntity(g: GlobalEntity): Ent {
    const kind = kindOf(g.class), K = kinds()[kind];
    const group = new THREE.Group(), body = new THREE.Group(), glyph = new THREE.Group();
    const solid = new THREE.Mesh(K.solid, new THREE.MeshLambertMaterial({ color: INK, transparent: true, opacity: 0, fog: false }));
    const outline = new THREE.LineSegments(K.outline, dottedMat(0));
    const ag = new THREE.BufferGeometry(); ag.setAttribute('position', new THREE.Float32BufferAttribute(new Float32Array(18), 3));
    const arrow = new THREE.LineSegments(ag, lineMat(0, 0));
    glyph.add(solid, outline);
    body.add(glyph, arrow);
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
    // Ring vertices are world positions draped on the terrain, rebuilt as the entity moves or the radius changes.
    const rg = new THREE.BufferGeometry(); rg.setAttribute('position', new THREE.BufferAttribute(new Float32Array((RING_N + 1) * 3), 3));
    const ring = new THREE.Line(rg, new THREE.LineDashedMaterial({ ...additive, color: INK, opacity: 0, dashSize: 1, gapSize: 0 }));
    const kg = new THREE.BufferGeometry(); kg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(8 * 3), 3));
    const ringTicks = new THREE.LineSegments(kg, lineMat(0, 0));
    ring.frustumCulled = false; ringTicks.frustumCulled = false; ring.visible = false; ringTicks.visible = false;
    group.add(body, drop, trail, ring, ringTicks);
    this.scene.add(group);
    return {
      gid: g.gid, cls: g.class, kind, group, body, glyph, yaw: 0, y0: 0, scale: 1, solid, outline, arrow, drop, trail, trailStyle: 0,
      ring, ringTicks, ringR: -1, ringTarget: null, ringStyle: 'live', ringKey: '',
      label, labelText: '', labelOpacity: 0, pts: [], coasting: false, coastSince: null, staleSince: null, dying: false,
      a: { solid: 0, tone: 0, outline: 0, arrow: 0, drop: 0, trail: 0, ring: 0 },
    };
  }

  private retire(e: Ent) { this.entities.delete(e.gid); e.dying = true; this.dying.push(e); }

  private dispose(e: Ent) {
    e.label.removeFromParent(); // CSS2DObject removes its element on 'removed'
    this.scene.remove(e.group);
    for (const o of [e.solid, e.outline, e.arrow, e.drop, e.trail, e.ring, e.ringTicks]) (o.material as THREE.Material).dispose();
    for (const o of [e.arrow, e.drop, e.trail, e.ring, e.ringTicks]) o.geometry.dispose();
  }

  private updateEntity(e: Ent, g: GlobalEntity, style: number, now: number) {
    const K = kinds()[e.kind];
    if (g.stale) e.staleSince ??= now; else e.staleSince = null;
    // Coasting (S15): every source device missed its heartbeat. Older servers do not send it (no ring either).
    e.coasting = !!g.coasting && !g.stale;
    if (g.coasting || g.stale) e.coastSince ??= now; else e.coastSince = null;
    e.ringTarget = typeof g.ce === 'number' && Number.isFinite(g.ce) && g.ce >= 0 ? g.ce : null;
    e.ringStyle = g.stale ? 'stale' : e.coasting ? 'coasting' : 'live';
    const [x, y0, z] = g.pos;
    e.y0 = y0;
    e.body.position.set(x, y0 + K.half * e.scale, z);
    const dp = e.drop.geometry.getAttribute('position') as THREE.BufferAttribute;
    dp.setXYZ(0, x, 0.012, z); dp.setXYZ(1, x, y0, z); dp.needsUpdate = true; e.drop.computeLineDistances();
    e.drop.geometry.setDrawRange(0, y0 > 0.05 ? 2 : 0);
    e.drop.geometry.computeBoundingSphere();

    // Velocity: a 1 px shaft with a chevron head, only when the entity is actually moving.
    const ap = e.arrow.geometry.getAttribute('position') as THREE.BufferAttribute;
    const v = new THREE.Vector3(...g.vel), len = v.length();
    // Vehicles turn to their heading. The box is symmetric, so it takes the nearer of the two ends
    // (no half-turns when a slow car's velocity flips); a new one starts on its heading.
    if (K.heads && Math.hypot(v.x, v.z) > HEADING_MIN) {
      const d = Math.atan2(-v.z, v.x) - e.yaw;
      e.yaw += d - Math.PI * Math.round(d / Math.PI);
      if (!e.pts.length) e.glyph.rotation.y = e.yaw;
    }
    if (len > ARROW_MIN) {
      // Room: one second of travel up to 2 m, from the centre. Wide: one second of travel in true
      // metres, kept between 12 and 90 px on screen, from the magnified glyph's edge.
      const px = 1 / this.pxPerM, wide = this.wideOn;
      const L = wide ? Math.min(90 * px, Math.max(12 * px, len)) : Math.min(len, 2), d = v.clone().divideScalar(len);
      const base = d.clone().multiplyScalar(wide ? K.reach * e.scale : 0), tip = base.clone().addScaledVector(d, L);
      const side = new THREE.Vector3(-d.z, 0, d.x); if (side.lengthSq() < 1e-6) side.set(1, 0, 0); side.normalize();
      const h = Math.min(wide ? 6 * px : 0.14, L * 0.4), back = tip.clone().addScaledVector(d, -h);
      const w1 = back.clone().addScaledVector(side, h * 0.55), w2 = back.clone().addScaledVector(side, -h * 0.55);
      [base.x, base.y, base.z, tip.x, tip.y, tip.z, tip.x, tip.y, tip.z, w1.x, w1.y, w1.z, tip.x, tip.y, tip.z, w2.x, w2.y, w2.z].forEach((c, i) => ((ap.array as Float32Array)[i] = c));
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

    const secs = (t: number) => `${((now - t) / 1000).toFixed(0)} s`;
    const state = e.staleSince !== null ? `stale ${secs(e.staleSince)}` : e.coasting && e.coastSince !== null ? `coasting ${secs(e.coastSince)}`
      : len > ARROW_MIN ? `${len.toFixed(1)} m/s` : 'still';
    const err = e.ringTarget !== null ? ` · ±${fmtRadius(e.ringTarget)}` : '';
    // Second line: the grid reference (S3) when the server has a geodetic anchor.
    const text = `${className(g.class)} · ${g.gid} · ${state}${err}${g.geo?.mgrs ? `\n${g.geo.mgrs}` : ''}`;
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
      // Float just above the body (bodies are opaque now), stem down to the ground; magnified with it in wide mode.
      const s = this.sigma[kindOf(en.class)], h = en.pos[1] + (2 * kinds()[kindOf(en.class)].half + 0.22) * s;
      gh.group.position.set(en.pos[0], h, en.pos[2]); gh.sphere.scale.setScalar(s);
      const sp = gh.stem.geometry.getAttribute('position') as THREE.BufferAttribute;
      sp.setY(0, -0.09 * s); sp.setY(1, -h + 0.012); sp.needsUpdate = true; gh.stem.computeLineDistances(); gh.stem.geometry.computeBoundingSphere();
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
        // Nadir (wide mode): world-aligned, so outside the device's rotated group.
        const ground = new THREE.Group(); ground.visible = false;
        const ng = new THREE.BufferGeometry(); ng.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3));
        const nadir = new THREE.Line(ng, dottedMat(0)); nadir.frustumCulled = false;
        const foot = new THREE.LineSegments(segs([-1, 0.03, 0, 1, 0.03, 0, 0, 0.03, -1, 0, 0.03, 1]), lineMat(0, 0));
        ground.add(nadir, foot);
        this.scene.add(group, ground); f = { group, line, label, style, silent: false, ground, nadir, foot, scale: 1 }; this.frustums.set(dk, f);
      }
      f.silent = d.silent;
      // Wide mode: the device's height above the marker plane (a drone at 60 m).
      const alt = this.wideOn && d.pose ? ` · alt ${Math.round(d.pose.pos[1])} m` : '';
      const text = `${d.provisional ? `dev ? · ${d.addr}` : `dev ${d.deviceId}`}${alt}${d.silent ? ' · lost' : d.coasting ? ' · coasting' : ''}`;
      if (f.label.element.textContent !== text) f.label.element.textContent = text;
      f.label.element.classList.toggle('lost', d.silent);
      if (d.pose) {
        f.group.position.set(...d.pose.pos); f.group.quaternion.set(...d.pose.quat); f.group.visible = true;
        const [x, y, z] = d.pose.pos, np = f.nadir.geometry.getAttribute('position') as THREE.BufferAttribute;
        f.ground.position.set(x, 0, z); np.setXYZ(0, 0, 0.03, 0); np.setXYZ(1, 0, y, 0); np.needsUpdate = true; f.nadir.computeLineDistances();
      } else f.group.visible = false;
    }
    for (const [k, f] of this.frustums) if (!seenDev.has(k)) {
      f.label.removeFromParent();
      this.scene.remove(f.group, f.ground);
      for (const o of [f.line, f.nadir, f.foot]) (o.material as THREE.Material).dispose();
      f.nadir.geometry.dispose(); f.foot.geometry.dispose();
      this.frustums.delete(k);
    }
    this.assess(snap, now);
  }

  // ---- wide mode ----
  /** Measure the entities' extent: switch modes (with hysteresis) and rescale the wide view. */
  private assess(snap: Snapshot, now: number) {
    const n = snap.global.length;
    if (n >= 2) {
      let cx = 0, cz = 0, minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
      for (const { pos: [x, , z] } of snap.global) {
        cx += x; cz += z;
        minX = Math.min(minX, x); maxX = Math.max(maxX, x); minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z);
      }
      cx /= n; cz /= n;
      const d = snap.global.map(g => Math.hypot(g.pos[0] - cx, g.pos[2] - cz)).sort((a, b) => a - b);
      const p90 = d[Math.ceil(0.9 * n) - 1];
      this.bounds = { minX, maxX, minZ, maxZ };
      this.spots = snap.global.flatMap(g => [g.pos[0], g.pos[2]]);
      const want = this.wideOn ? p90 > WIDE_EXIT_M : p90 > WIDE_ENTER_M;
      if (want === this.wideOn) this.flipSince = null;
      else if (!this.measured) this.setWide(want, false); // the first look decides at once, without a glide
      else if (now - (this.flipSince ??= now) >= (want ? WIDE_ENTER_MS : WIDE_EXIT_MS)) { this.flipSince = null; this.setWide(want, true); }
      this.measured = true;
      if (this.wideOn) {
        this.rescale();
        const f = this.framed, grow = 0.1 * Math.max(WIDE_ENTER_M, f.maxX - f.minX, f.maxZ - f.minZ);
        if (now < this.settleUntil && (minX < f.minX - grow || maxX > f.maxX + grow || minZ < f.minZ - grow || maxZ > f.maxZ + grow)) this.frame(true, this.framedBearing);
      }
    } else this.flipSince = null; // fewer than two entities: nothing to measure, the mode holds
    const cell = this.wideOn ? this.wideR / 10 : 0;
    if (cell !== this.emittedCell) { this.emittedCell = cell; this.onWide?.(this.wideOn, cell); }
  }

  private setWide(on: boolean, glide: boolean) {
    this.wideOn = on;
    this.terrain.visible = !on;
    if (on) {
      this.roomView ??= { pos: this.camera.position.clone(), target: this.controls.target.clone() };
      this.wideR = 0; this.rescale();
      // Start from where the (first) device looks, so the twin reads like its footage; F keeps the operator's bearing.
      const dev = [...this.frustums.values()].find(f => f.group.visible)?.group.position;
      const b = this.bounds, c = new THREE.Vector3((b.minX + b.maxX) / 2, 0, (b.minZ + b.maxZ) / 2);
      this.frame(glide, dev && Math.hypot(dev.x - c.x, dev.z - c.z) > 1 ? Math.atan2(dev.x - c.x, dev.z - c.z) : undefined);
      this.settleUntil = performance.now() + WIDE_SETTLE_MS;
      return;
    }
    if (this.grid) { this.scene.remove(this.grid); this.grid.geometry.dispose(); (this.grid.material as THREE.Material).dispose(); this.grid = null; }
    this.wideR = 0;
    const fog = this.scene.fog as THREE.Fog; fog.near = ROOM_FOG[0]; fog.far = ROOM_FOG[1];
    const back = this.roomView ?? { pos: ROOM_POS, target: ROOM_TARGET };
    this.roomView = null;
    // Far plane and zoom limit return once the camera is back in the room.
    this.glideTo(back, glide, () => { this.camera.far = ROOM_FAR; this.camera.updateProjectionMatrix(); this.controls.maxDistance = ROOM_MAX_DIST; });
  }

  /** Grid, fog, far plane and zoom limit for the entities' extent: in WIDE_STEPS, growing at once, shrinking with hysteresis. */
  private rescale() {
    const b = this.bounds, r = Math.max(WIDE_ENTER_M, Math.hypot(b.maxX - b.minX, b.maxZ - b.minZ) / 2);
    const step = WIDE_STEPS.find(s => s >= r) ?? WIDE_STEPS[WIDE_STEPS.length - 1];
    if (step > this.wideR || r < 0.4 * this.wideR) this.wideR = step;
    const R = this.wideR, cell = R / 10, bx = (b.minX + b.maxX) / 2, bz = (b.minZ + b.maxZ) / 2;
    // The grid follows the entities when they drift half a scale radius away; its lines stay on the world's cells.
    if (this.grid && this.gridAt.r === R && Math.hypot(bx - this.gridAt.x, bz - this.gridAt.z) <= R / 2) return;
    this.gridAt = { x: Math.round(bx / cell) * cell, z: Math.round(bz / cell) * cell, r: R };
    if (this.grid) { this.scene.remove(this.grid); this.grid.geometry.dispose(); (this.grid.material as THREE.Material).dispose(); }
    this.grid = buildGrid(this.gridAt.x, this.gridAt.z, 2 * R, cell);
    this.scene.add(this.grid);
    const fog = this.scene.fog as THREE.Fog; fog.near = 2 * R; fog.far = 7 * R;
    // A smaller scale never pulls the camera in (that would fight the operator's zoom).
    const dist = this.camera.position.distanceTo(this.controls.target);
    this.camera.far = Math.max(20 * R, 4 * dist); this.camera.updateProjectionMatrix();
    this.controls.maxDistance = Math.max(6 * R, dist);
  }

  /**
   * Wide mode (F, the FRAME control): put the entities in view from `bearing` (radians about +y;
   * default the operator's current one, or where a glide is heading) at WIDE_ELEV, and the devices
   * above them unless that would push the view out by more than half again.
   */
  frame(glide = true, bearing?: number) {
    if (!this.wideOn) return;
    const view = this.glide?.to ?? { pos: this.camera.position, target: this.controls.target };
    bearing ??= new THREE.Spherical().setFromVector3(view.pos.clone().sub(view.target)).theta;
    const b = this.bounds; this.framed = { ...b }; this.framedBearing = bearing;
    const target = new THREE.Vector3((b.minX + b.maxX) / 2, 0, (b.minZ + b.maxZ) / 2);
    const ents: THREE.Vector3[] = [];
    for (let i = 0; i < this.spots.length; i += 2) ents.push(new THREE.Vector3(this.spots[i], 0, this.spots[i + 1]), new THREE.Vector3(this.spots[i], 4, this.spots[i + 1]));
    const devs = [...this.frustums.values()].filter(f => f.group.visible).map(f => f.group.position.clone());
    const dir = new THREE.Vector3().setFromSphericalCoords(1, Math.PI / 2 - WIDE_ELEV, bearing);
    // Move out until every point sits inside FRAME_FILL of the view (perspective: a few fixed-point steps).
    const cam = new THREE.PerspectiveCamera(this.camera.fov, this.camera.aspect, 0.1, 1e6);
    const fit = (pts: THREE.Vector3[]) => {
      let d = Math.max(20, Math.hypot(b.maxX - b.minX, b.maxZ - b.minZ));
      for (let i = 0; i < 12; i++) {
        cam.position.copy(target).addScaledVector(dir, d); cam.lookAt(target); cam.updateMatrixWorld();
        let m = 0;
        for (const p of pts) {
          const c = p.clone().applyMatrix4(cam.matrixWorldInverse);
          if (c.z > -1) { m = Infinity; break; } // behind or at the camera: further out
          const v = c.applyMatrix4(cam.projectionMatrix);
          m = Math.max(m, Math.abs(v.x), Math.abs(v.y));
        }
        d *= Number.isFinite(m) ? m / FRAME_FILL : 2;
      }
      return d;
    };
    const near = fit(ents), d = Math.min(Math.max(devs.length ? Math.min(fit([...ents, ...devs]), 1.5 * near) : near, this.controls.minDistance), this.controls.maxDistance);
    this.glideTo({ pos: target.clone().addScaledVector(dir, d), target }, glide);
  }

  /** Move the orbit to `to`, at once or over GLIDE_MS (target and angles eased, distance on a log scale). */
  private glideTo(to: View, glide: boolean, done?: () => void) {
    this.glide = null; // a newer framing replaces an unfinished one, including its limits
    if (!glide) { this.camera.position.copy(to.pos); this.controls.target.copy(to.target); done?.(); return; }
    this.glide = { t0: performance.now(), from: { pos: this.camera.position.clone(), target: this.controls.target.clone() }, to, done };
  }

  private stepGlide(now: number) {
    const g = this.glide;
    if (!g) return;
    const t = Math.min(1, Math.max(0, (now - g.t0) / GLIDE_MS)), e = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
    const a = new THREE.Spherical().setFromVector3(g.from.pos.clone().sub(g.from.target));
    const b = new THREE.Spherical().setFromVector3(g.to.pos.clone().sub(g.to.target));
    const dTheta = b.theta - a.theta - 2 * Math.PI * Math.round((b.theta - a.theta) / (2 * Math.PI));
    const s = new THREE.Spherical(a.radius * Math.pow(b.radius / a.radius, e), a.phi + (b.phi - a.phi) * e, a.theta + dTheta * e);
    this.controls.target.lerpVectors(g.from.target, g.to.target, e);
    this.camera.position.setFromSpherical(s).add(this.controls.target);
    if (t >= 1) this.endGlide();
  }

  /** Finish a glide where it is (the operator took over) or where it ends, then apply its limits. */
  private endGlide() {
    const g = this.glide;
    this.glide = null;
    g?.done?.();
  }

  /** Screen px per metre at a point (CSS px, at its distance from the camera). */
  private pxAt(p: THREE.Vector3): number {
    const dist = Math.max(0.01, this.camera.position.distanceTo(p));
    return Math.max(1, this.container.clientHeight) / (2 * dist * Math.tan(THREE.MathUtils.degToRad(this.camera.fov) / 2));
  }
  /** Symbol scales ease (~250 ms) so a mode switch or a zoom never jumps them; they land exactly. */
  private ease(cur: number, want: number): number {
    const v = cur + (want - cur) * this.easeK;
    return Math.abs(v - want) < 1e-3 * want ? want : v;
  }

  /** Per frame: px per metre at the orbit target, and from it the glyph and dash scales (1 in room mode). */
  private scaleSymbols(dt: number) {
    this.easeK = 1 - Math.exp(-dt / 0.08);
    if (this.container.clientHeight < 1) return;
    this.pxPerM = this.pxAt(this.controls.target);
    const w = this.wideOn;
    for (const kind of KINDS) { const K = kinds()[kind]; this.sigma[kind] = this.ease(this.sigma[kind], w ? Math.max(1, K.minPx / (K.size * this.pxPerM)) : 1); }
    this.dashQ = this.ease(this.dashQ, w ? Math.max(1, ROOM_PX_PER_M / this.pxPerM) : 1);
  }
}
