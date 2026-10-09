import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import type { Snapshot } from './types';

const CLASS_COLOR: Record<number, number> = { 0: 0x58a6ff, 56: 0xd29922, 41: 0x3fb950, 39: 0x3fb950, 63: 0xbc8cff, 24: 0xf778ba, 26: 0xf778ba, 67: 0xbc8cff, 62: 0xbc8cff };
const DEVICE_COLORS = [0xff7b72, 0x79c0ff, 0x56d364, 0xe3b341];

export class TwinScene {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private entities = new Map<string, { cls: number; mesh: THREE.Mesh; arrow: THREE.ArrowHelper; trail: THREE.Line; pts: THREE.Vector3[] }>();
  private frustums = new Map<string, THREE.Group>();
  private ghosts = new Map<string, THREE.Mesh>();
  private deviceColor = new Map<string, number>();
  showGhosts = false;

  constructor(container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(devicePixelRatio);
    container.appendChild(this.renderer.domElement);
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.1, 200);
    this.camera.position.set(6, 8, 6);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.scene.background = new THREE.Color(0x0d1117);
    this.scene.add(new THREE.GridHelper(20, 20, 0x30363d, 0x21262d));
    const marker = new THREE.Mesh(new THREE.PlaneGeometry(0.42, 0.3), new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.DoubleSide }));
    marker.rotation.x = -Math.PI / 2; marker.position.y = 0.002; this.scene.add(marker);
    this.scene.add(new THREE.AxesHelper(1));
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x222233, 1.2));
    const resize = () => { const w = container.clientWidth, h = container.clientHeight; this.renderer.setSize(w, h); this.camera.aspect = w / h; this.camera.updateProjectionMatrix(); };
    new ResizeObserver(resize).observe(container); resize();
    const loop = () => { this.controls.update(); this.renderer.render(this.scene, this.camera); requestAnimationFrame(loop); }; loop();
  }

  /** Stable colour per device for its lifetime in this view. */
  private colorOf(key: string): number {
    let c = this.deviceColor.get(key);
    if (c === undefined) { c = DEVICE_COLORS[this.deviceColor.size % DEVICE_COLORS.length]; this.deviceColor.set(key, c); }
    return c;
  }

  private removeEntity(gid: string) {
    const e = this.entities.get(gid); if (!e) return;
    this.scene.remove(e.mesh, e.arrow, e.trail);
    e.mesh.geometry.dispose(); (e.mesh.material as THREE.Material).dispose(); e.trail.geometry.dispose(); e.arrow.dispose();
    this.entities.delete(gid);
  }

  update(snap: Snapshot) {
    const seen = new Set<string>();
    // Global (fused) entities are what the operator sees; per-device views become ghosts.
    for (const g of snap.global) {
      seen.add(g.gid);
      let e = this.entities.get(g.gid);
      if (e && e.cls !== g.class) { this.removeEntity(g.gid); e = undefined; } // re-classified, or gid reused after a server restart
      if (!e) {
        const color = CLASS_COLOR[g.class] ?? 0x8b949e;
        const mesh = new THREE.Mesh(g.class === 0 ? new THREE.CapsuleGeometry(0.2, 1.3, 4, 8) : new THREE.BoxGeometry(0.4, 0.4, 0.4), new THREE.MeshStandardMaterial({ color, transparent: true }));
        const arrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(), 1, color);
        const trail = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.5 }));
        this.scene.add(mesh, arrow, trail);
        e = { cls: g.class, mesh, arrow, trail, pts: [] }; this.entities.set(g.gid, e);
      }
      const y = g.class === 0 ? g.pos[1] + 0.85 : g.pos[1] + 0.2;
      e.mesh.position.set(g.pos[0], y, g.pos[2]);
      // Stale = not refreshed for 2 x T_max (or its device went silent): faded wireframe.
      const mat = e.mesh.material as THREE.MeshStandardMaterial;
      mat.opacity = g.stale ? 0.35 : 1; mat.wireframe = g.stale;
      for (const part of [e.arrow.line, e.arrow.cone]) { const am = part.material as THREE.Material; am.transparent = true; am.opacity = g.stale ? 0.3 : 1; }
      const v = new THREE.Vector3(...g.vel); const len = v.length();
      e.arrow.position.set(g.pos[0], y, g.pos[2]); e.arrow.visible = len > 0.05;
      if (len > 0.05) { e.arrow.setDirection(v.normalize()); e.arrow.setLength(Math.min(len, 2), 0.15, 0.1); }
      e.pts.push(new THREE.Vector3(g.pos[0], g.pos[1] + 0.02, g.pos[2])); if (e.pts.length > 150) e.pts.shift();
      e.trail.geometry.setFromPoints(e.pts);
    }
    for (const gid of [...this.entities.keys()]) if (!seen.has(gid)) this.removeEntity(gid);

    const seenGhosts = new Set<string>();
    if (this.showGhosts) for (const d of snap.devices) for (const en of d.entities) {
      const dk = d.key ?? String(d.deviceId);
      const k = `${dk}#${en.id}`; seenGhosts.add(k);
      let m = this.ghosts.get(k);
      if (!m) { m = new THREE.Mesh(new THREE.SphereGeometry(0.12, 8, 8), new THREE.MeshBasicMaterial({ color: this.colorOf(dk), wireframe: true, transparent: true })); this.scene.add(m); this.ghosts.set(k, m); }
      m.position.set(en.pos[0], en.pos[1] + 0.3, en.pos[2]);
      (m.material as THREE.MeshBasicMaterial).opacity = en.stale ? 0.3 : 1;
    }
    for (const [k, m] of this.ghosts) if (!seenGhosts.has(k)) { this.scene.remove(m); m.geometry.dispose(); (m.material as THREE.Material).dispose(); this.ghosts.delete(k); }

    const seenDev = new Set<string>();
    for (const d of snap.devices) {
      const dk = d.key ?? String(d.deviceId);
      seenDev.add(dk);
      let f = this.frustums.get(dk);
      if (!f) {
        f = new THREE.Group();
        const cam = new THREE.PerspectiveCamera(60, 4 / 3, 0.1, 1.5);
        const helper = new THREE.CameraHelper(cam); (helper.material as THREE.LineBasicMaterial).color.setHex(this.colorOf(dk));
        f.add(cam, helper); this.scene.add(f); this.frustums.set(dk, f);
      }
      if (d.pose) { f.position.set(...d.pose.pos); f.quaternion.set(...d.pose.quat); f.visible = true; } else f.visible = false;
    }
    for (const [k, f] of this.frustums) if (!seenDev.has(k)) { this.scene.remove(f); this.frustums.delete(k); }
  }
}
