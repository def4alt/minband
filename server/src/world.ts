// World model: one WASM Receiver per device, device identity, clock offset, metrics, fusion and
// twin-error bookkeeping.
//
// Device identity: a device is keyed by `id:<device_id>` once its Hello has been seen, and by
// `addr:<ip:port>` before that (provisional). When a known device_id says Hello from a new
// address, the same Device object (receiver state, clock, metrics) moves there; the old address
// stays routed to it as an alias, and acks always go to the latest address.
// An acked edge never repeats Hello, so a phone whose UDP port changes after a Wi-Fi hiccup
// would otherwise come back as an anonymous device. A provisional device is therefore adopted by
// an identified device when (a) the identified device went quiet no later than the newcomer
// appeared and has been silent for ADOPT_SILENCE_MS, (b) the newcomer's first edge tick
// continues the old tick stream within ADOPT_TICK_TOLERANCE, and (c) exactly one device matches.
//
// Acks are only generated for datagrams that got through the shaper, so an emulated blackout
// also silences the downlink, and a Hello dropped by the shaper does not get the edge acked (an
// acked edge stops sending Hello, and the server would never learn its device_id).
//
// Packet events (V3): every datagram arriving at the socket (dropped by the shaper or not) and
// every ack sent is queued as a PacketEvent and drained into the next snapshot.
import { WasmReceiver } from 'minband-core';
import { ClockOffset } from './clock.js';
import { Fusion } from './fusion.js';
import { Shaper, UDP_IP_OVERHEAD } from './shaper.js';
import { peek, type Peek } from './peek.js';
import { Link, NO_AIRTIME, airtimeMs, type LinkDeps } from './link.js';
import { SnapshotRing, evaluateTwin, parseGroundTruthCsv, summarize, type TwinEvaluation } from './groundtruth.js';
import type { AirtimeModel, DeviceView, EntityView, LinkView, PacketEvent, PoseView, Snapshot, TwinError } from './types.js';
import { TICK_HZ } from './types.js';

export const DEVICE_SILENT_MS = 5_000;
export const DEVICE_TIMEOUT_MS = 30_000;
export const ACK_INTERVAL_MS = 100;
export const ADOPT_SILENCE_MS = 500;
export const ADOPT_WINDOW_MS = 5_000;
export const ADOPT_TICK_TOLERANCE = TICK_HZ;
const RATE_WINDOW_MS = 2_000;
const LOG_LINES = 200;
const BASELINES = { h264_720p_bps: 1_500_000, h264_480p_bps: 500_000, naiveMetadataBps: 0 };
/** Packet events kept between snapshots (oldest dropped first). */
export const MAX_PACKETS = 2_000;
/** Until core's peek_json lands, Peek has no `ids`. */
type PeekIds = Peek & { ids?: number[] };

/** Sliding-window byte/message counter. */
export class RateWindow {
  private t: number[] = []; private b: number[] = []; private head = 0; private sum = 0;
  constructor(readonly windowMs = RATE_WINDOW_MS) {}
  push(t: number, bytes: number) { this.t.push(t); this.b.push(bytes); this.sum += bytes; }
  private prune(nowMs: number) {
    while (this.head < this.t.length && nowMs - this.t[this.head] >= this.windowMs) { this.sum -= this.b[this.head]; this.head++; }
    if (this.head > 1024 && this.head * 2 > this.t.length) { this.t = this.t.slice(this.head); this.b = this.b.slice(this.head); this.head = 0; }
  }
  rate(nowMs: number): { bps: number; msgsPerSec: number } {
    this.prune(nowMs);
    const s = this.windowMs / 1000;
    return { bps: this.sum * 8 / s, msgsPerSec: (this.t.length - this.head) / s };
  }
  /** Share of the window's wall time its datagrams occupy on air under `m` (sizes here include the
   * UDP/IP header, which is not on air). Recomputed per call, so a new model applies at once. */
  airtimeShare(nowMs: number, m: AirtimeModel): number {
    if (m.kind === 'none') return 0;
    this.prune(nowMs);
    let ms = 0;
    for (let i = this.head; i < this.b.length; i++) ms += airtimeMs(m, this.b[i] - UDP_IP_OVERHEAD);
    return ms / this.windowMs;
  }
}

export class Device {
  rx = new WasmReceiver();
  clock = new ClockOffset();
  deviceId: number | null = null;
  nonce: number | null = null;
  /** Earlier addresses still routed here. Acks go to `addr` only. */
  aliases = new Set<string>();
  addrChanges = 0;
  sessions = 1;
  delivered = new RateWindow();
  offered = new RateWindow();
  firstSeenMs: number;
  firstTick: number | null = null;
  lastSeenMs: number;
  lastAckMs = -Infinity;
  constructor(public addr: string, nowMs: number) { this.firstSeenMs = nowMs; this.lastSeenMs = nowMs; }

  get key(): string { return this.deviceId === null ? `addr:${this.addr}` : `id:${this.deviceId}`; }
  get provisional(): boolean { return this.deviceId === null; }

  /** Edge restarted (new session nonce): the core Receiver keeps the old last_edge_tick across a
   * new Hello, so start from a fresh one rather than mixing two tick bases. */
  newSession() {
    this.rx.free(); this.rx = new WasmReceiver();
    this.clock.reset(); this.firstTick = null; this.lastAckMs = -Infinity; this.sessions++;
  }

  ingest(buf: Uint8Array, nowMs: number): string {
    const ev = this.rx.on_datagram(buf); // throws on malformed input
    this.lastSeenMs = nowMs;
    this.delivered.push(nowMs, buf.length + UDP_IP_OVERHEAD);
    const tick = this.rx.last_edge_tick();
    if (this.firstTick === null) this.firstTick = tick;
    this.clock.sample(nowMs, tick / TICK_HZ * 1000);
    return ev;
  }

  edgeTickNow(nowMs: number): number {
    const e = this.clock.edgeMsAt(nowMs);
    return e === null ? this.rx.last_edge_tick() : Math.max(0, Math.round(e / 1000 * TICK_HZ));
  }

  view(nowMs: number, model: AirtimeModel = NO_AIRTIME): DeviceView {
    const tick = this.edgeTickNow(nowMs);
    this.rx.gc(tick);
    const silent = nowMs - this.lastSeenMs > DEVICE_SILENT_MS;
    let entities = JSON.parse(this.rx.extrapolate_json(tick)) as EntityView[];
    if (silent) entities = entities.map(e => ({ ...e, stale: true }));
    const poseJson = this.rx.pose_json();
    const pose = poseJson ? (JSON.parse(poseJson) as PoseView) : null;
    const d = this.delivered.rate(nowMs), o = this.offered.rate(nowMs);
    return {
      deviceId: this.deviceId ?? 0, addr: this.addr, entities, pose, bps: d.bps, msgsPerSec: d.msgsPerSec,
      stats: JSON.parse(this.rx.stats_json()), lastSeenMs: this.lastSeenMs,
      key: this.key, provisional: this.provisional, offeredBps: o.bps, edgeTick: tick, silent,
      addrChanges: this.addrChanges, clockOffsetMs: this.clock.offsetMs,
      airtimeShare: this.delivered.airtimeShare(nowMs, model),
    };
  }

  free() { this.rx.free(); }
}

export interface WorldOptions {
  now?: () => number; shaper?: Shaper;
  /** Timers and RNG for the contested loop (tests pass a fake clock). */
  link?: Pick<LinkDeps, 'rng' | 'schedule' | 'cancel'>;
}
type TwinRecord = TwinEvaluation & { updatedMs: number };

export class World {
  readonly now: () => number;
  /** key (`id:<device_id>` or `addr:<ip:port>`) -> device. */
  readonly devices = new Map<string, Device>();
  /** Every address (latest and aliases) -> device. */
  private readonly byAddr = new Map<string, Device>();
  readonly fusion = new Fusion();
  readonly shaper: Shaper;
  readonly link: Link;
  /** Link budget (0 = unlimited), split over the live devices in each ack: see `edgeBudget`. */
  budgetBps = 0;
  log: string[] = [];
  /** Packet events since the last snapshot (V3). */
  packets: PacketEvent[] = [];
  /** Called with (address, ack datagram) whenever an ack is due. */
  onAck: (addr: string, ack: Uint8Array) => void = () => {};
  /** Per device_id: what the twin served, 30 Hz, last 60 s (outlives the Device for late GT uploads). */
  readonly rings = new Map<number, SnapshotRing>();
  readonly twin = new Map<number, TwinRecord>();
  lastSnapshot: Snapshot | null = null;
  private readonly unattributed = new RateWindow();
  /** Acks sent, all devices (downlink airtime). */
  private readonly acksOut = new RateWindow();
  readonly startedMs: number;

  constructor(opts: WorldOptions = {}) {
    this.now = opts.now ?? Date.now;
    this.shaper = opts.shaper ?? new Shaper({ now: this.now });
    this.link = new Link({ ...opts.link, shaper: this.shaper, now: this.now, setBudget: b => { this.budgetBps = b; }, note: l => this.note(this.now(), l) });
    this.startedMs = this.now();
  }

  device(deviceId: number): Device | undefined { return this.devices.get(`id:${deviceId}`); }
  deviceAt(addr: string): Device | undefined { return this.byAddr.get(addr); }

  /** Every raw datagram from the socket. Goes through the shaper, then to its device. */
  ingest(addr: string, buf: Uint8Array): void {
    const nowMs = this.now();
    const known = this.byAddr.get(addr);
    (known?.offered ?? this.unattributed).push(nowMs, buf.length + UDP_IP_OVERHEAD);
    const p: PeekIds = peek(buf);
    // A Hello names its device before it is routed; anything else belongs to the address's owner.
    const up: PacketEvent = { t: nowMs, dir: 'up', key: p.kind === 'hello' ? `id:${p.deviceId}` : known?.key ?? '', kind: p.kind, bytes: buf.length + UDP_IP_OVERHEAD, dropped: false };
    if (p.seq !== undefined) up.seq = p.seq;
    if (p.ids) up.ids = p.ids;
    this.packet(up);
    if (!this.shaper.offer(buf.length, () => this.deliver(addr, buf, p, up, this.now()))) up.dropped = true;
  }

  /** Budget each edge is told: the link budget split evenly over the devices heard in the last
   * DEVICE_SILENT_MS. A profile's budget is the link's; an edge under its budget tightens its
   * thresholds toward the floor (one walker at 8 kbit/s: ~330 B/s instead of ~130), so N edges each
   * told the whole budget oversubscribe the link N times. One device: unchanged. */
  edgeBudget(nowMs = this.now()): number {
    if (this.budgetBps <= 0) return 0;
    let n = 0;
    for (const d of this.devices.values()) if (nowMs - d.lastSeenMs <= DEVICE_SILENT_MS) n++;
    return Math.max(1, Math.floor(this.budgetBps / Math.max(1, n)));
  }

  private packet(ev: PacketEvent) {
    if (this.packets.length >= MAX_PACKETS) this.packets.shift();
    this.packets.push(ev);
  }

  private deliver(addr: string, buf: Uint8Array, p: Peek, up: PacketEvent, nowMs: number) {
    if (p.kind === 'malformed' || p.kind === 'ack') { this.note(nowMs, `${addr} ignored: ${p.text}`); return; }
    const dev = this.route(addr, p, nowMs);
    up.key = dev.key; // identified or adopted on the way in (moot if a snapshot already carried it)
    let ev: string;
    try { ev = dev.ingest(buf, nowMs); } catch (e) { this.note(nowMs, `${addr} malformed: ${e}`); return; }
    this.note(nowMs, `${addr} ${ev}`);
    if (dev.rx.needs_ack() || nowMs - dev.lastAckMs >= ACK_INTERVAL_MS) {
      dev.lastAckMs = nowMs;
      const ack = dev.rx.make_ack(this.edgeBudget(nowMs));
      this.acksOut.push(nowMs, ack.length + UDP_IP_OVERHEAD);
      this.packet({ t: nowMs, dir: 'down', key: dev.key, kind: 'ack', bytes: ack.length + UDP_IP_OVERHEAD, dropped: false });
      this.onAck(dev.addr, ack);
    }
  }

  private route(addr: string, p: Peek, nowMs: number): Device {
    let dev = this.byAddr.get(addr);
    if (p.kind === 'hello') {
      const id = p.deviceId!, nonce = p.nonce!;
      const known = this.device(id);
      if (known) {
        if (dev && dev !== known) this.releaseAddr(addr, dev);
        if (known.addr !== addr) this.migrate(known, addr, 'hello from new address');
        dev = known;
      } else {
        if (dev && !dev.provisional) { this.releaseAddr(addr, dev); dev = undefined; } // address reused by another device
        if (!dev) dev = this.create(addr, nowMs);
        else if (p.tick !== undefined && p.tick < dev.rx.last_edge_tick()) dev.newSession(); // provisional, ticks restarted
        this.identify(dev, id);
      }
      if (dev.nonce !== null && dev.nonce !== nonce) {
        dev.newSession(); this.rings.get(id)?.clear();
        this.note(nowMs, `dev ${id} new session (nonce ${nonce})`);
      }
      dev.nonce = nonce;
      return dev;
    }
    if (!dev) {
      const c = this.adoptionCandidate(nowMs, p.tick, nowMs);
      if (c) { this.migrate(c, addr, 'adopted: continues its tick stream'); return c; }
      return this.create(addr, nowMs);
    }
    if (dev.provisional && dev.firstTick !== null && nowMs - dev.firstSeenMs <= ADOPT_WINDOW_MS) {
      const c = this.adoptionCandidate(dev.firstSeenMs, dev.firstTick, nowMs);
      if (c) { this.removeDevice(dev); this.migrate(c, addr, 'adopted: continues its tick stream'); return c; }
    }
    return dev;
  }

  private adoptionCandidate(firstSeenMs: number, firstTick: number | undefined, nowMs: number): Device | null {
    if (firstTick === undefined) return null;
    let found: Device | null = null;
    for (const c of this.devices.values()) {
      if (c.provisional) continue;
      if (c.lastSeenMs > firstSeenMs + 50) continue; // still talking after the newcomer appeared
      if (nowMs - c.lastSeenMs < ADOPT_SILENCE_MS) continue;
      const last = c.rx.last_edge_tick();
      const expected = last + (firstSeenMs - c.lastSeenMs) / 1000 * TICK_HZ;
      if (firstTick < last || Math.abs(firstTick - expected) > ADOPT_TICK_TOLERANCE) continue;
      if (found) return null; // ambiguous: keep them apart
      found = c;
    }
    return found;
  }

  private create(addr: string, nowMs: number): Device {
    const dev = new Device(addr, nowMs);
    this.devices.set(dev.key, dev); this.byAddr.set(addr, dev);
    return dev;
  }

  private identify(dev: Device, id: number) {
    if (this.devices.get(dev.key) === dev) this.devices.delete(dev.key);
    dev.deviceId = id;
    this.devices.set(dev.key, dev);
  }

  private migrate(dev: Device, addr: string, why: string) {
    const other = this.byAddr.get(addr);
    if (other && other !== dev) this.releaseAddr(addr, other);
    const prev = dev.addr;
    dev.aliases.add(prev); dev.aliases.delete(addr);
    dev.addr = addr; this.byAddr.set(addr, dev); dev.addrChanges++;
    this.note(this.now(), `dev ${dev.deviceId} moved ${prev} -> ${addr} (${why})`);
  }

  private releaseAddr(addr: string, dev: Device) {
    if (this.byAddr.get(addr) === dev) this.byAddr.delete(addr);
    dev.aliases.delete(addr);
    if (dev.addr !== addr) return;
    if (dev.provisional) { this.removeDevice(dev); return; }
    const alt = dev.aliases.values().next().value as string | undefined;
    if (alt !== undefined) { dev.aliases.delete(alt); dev.addr = alt; }
  }

  private removeDevice(dev: Device) {
    if (this.devices.get(dev.key) === dev) this.devices.delete(dev.key);
    for (const a of [dev.addr, ...dev.aliases]) if (this.byAddr.get(a) === dev) this.byAddr.delete(a);
    dev.free();
  }

  private note(nowMs: number, line: string) {
    if (this.log.length >= LOG_LINES) this.log.shift();
    this.log.push(`${new Date(nowMs).toISOString().slice(11, 23)} ${line}`);
  }

  private expire(nowMs: number) {
    for (const d of [...this.devices.values()]) if (nowMs - d.lastSeenMs > DEVICE_TIMEOUT_MS) {
      this.note(nowMs, `${d.key} timed out`);
      this.removeDevice(d);
    }
  }

  private ring(deviceId: number): SnapshotRing {
    let r = this.rings.get(deviceId);
    if (!r) { r = new SnapshotRing(); this.rings.set(deviceId, r); }
    return r;
  }

  views(nowMs = this.now()): DeviceView[] { return [...this.devices.values()].map(d => d.view(nowMs, this.link.model)); }

  /** Profile, airtime model, and uplink/downlink channel use over the rate window, all devices. */
  linkView(nowMs = this.now(), views?: DeviceView[]): LinkView {
    let airtimeShare = 0, msgsPerSec = 0;
    if (views) for (const v of views) { airtimeShare += v.airtimeShare; msgsPerSec += v.msgsPerSec; }
    else for (const d of this.devices.values()) { airtimeShare += d.delivered.airtimeShare(nowMs, this.link.model); msgsPerSec += d.delivered.rate(nowMs).msgsPerSec; }
    const down = { airtimeShare: this.acksOut.airtimeShare(nowMs, this.link.model), msgsPerSec: this.acksOut.rate(nowMs).msgsPerSec };
    return this.link.view({ airtimeShare, msgsPerSec }, down);
  }

  /** Called at 30 Hz: the WS snapshot; also records what the twin served for twin-error. */
  snapshot(nowMs = this.now()): Snapshot {
    this.expire(nowMs);
    const devices = this.views(nowMs);
    for (const v of devices) if (!v.provisional) this.ring(v.deviceId).push(v.edgeTick, v.entities);
    const global = this.fusion.update(devices, nowMs);
    const entityCount = devices.reduce((a, d) => a + d.entities.length, 0);
    const snap: Snapshot = {
      t: nowMs, devices, global, shaper: this.shaper.config, fusion: this.fusion.enabled,
      baselines: { ...BASELINES, naiveMetadataBps: entityCount * 31 * 30 * 8 + 30 * 40 * 8 },
      budgetBps: this.budgetBps, shaperRevertMs: this.shaper.revertInMs(),
      link: this.linkView(nowMs, devices), packets: this.packets.splice(0),
    };
    this.lastSnapshot = snap;
    return snap;
  }

  /** Evaluate an uploaded ground-truth CSV against what the twin served. Null: unknown device. */
  postGroundTruth(deviceId: number, csv: string, nowMs = this.now()) {
    const ring = this.rings.get(deviceId);
    if (!ring) return null;
    const { rows, malformed } = parseGroundTruthCsv(csv);
    const ev = evaluateTwin(rows, ring);
    this.twin.set(deviceId, { ...ev, updatedMs: nowMs });
    const { distances: _d, ...rest } = ev;
    return { deviceId, malformed, ...rest };
  }

  twinError(deviceId?: number): TwinError {
    const recs = deviceId === undefined ? [...this.twin.values()] : [this.twin.get(deviceId)].filter((r): r is TwinRecord => !!r);
    const n = recs.reduce((a, r) => a + r.distances.length, 0);
    const all = new Float64Array(n); let o = 0;
    for (const r of recs) { all.set(r.distances, o); o += r.distances.length; }
    return summarize(all);
  }

  metrics(nowMs = this.now()) {
    const devs = [...this.devices.values()];
    const views = devs.map(d => d.view(nowMs, this.link.model));
    const deviceEntityCount = views.reduce((a, v) => a + v.entities.length, 0);
    return {
      t: nowMs,
      uptimeS: (nowMs - this.startedMs) / 1000,
      entityCount: this.lastSnapshot?.global.length ?? deviceEntityCount,
      deviceEntityCount,
      devices: devs.map((d, i) => {
        const v = views[i]; const tw = d.deviceId !== null ? this.twin.get(d.deviceId) : undefined;
        return {
          key: v.key, deviceId: d.deviceId, provisional: v.provisional, addr: d.addr, aliases: [...d.aliases],
          bps: v.bps, offeredBps: v.offeredBps, msgsPerSec: v.msgsPerSec, airtimeShare: v.airtimeShare,
          entities: v.entities.length, staleEntities: v.entities.filter(e => e.stale).length,
          stats: v.stats, lastSeenMs: d.lastSeenMs, silentMs: nowMs - d.lastSeenMs, silent: v.silent,
          edgeTick: v.edgeTick, lastEdgeTick: d.rx.last_edge_tick(),
          clock: { offsetMs: d.clock.offsetMs, windowMinMs: d.clock.windowMinMs, samples: d.clock.samples, steps: d.clock.steps },
          addrChanges: d.addrChanges, sessions: d.sessions,
          twinError: tw ? { meanM: tw.meanM, p95M: tw.p95M, samples: tw.samples, rows: tw.rows, skipped: tw.skipped, missing: tw.missing, updatedMs: tw.updatedMs } : null,
        };
      }),
      unattributedBps: this.unattributed.rate(nowMs).bps,
      shaper: { config: this.shaper.config, counters: { ...this.shaper.counters }, revertInMs: this.shaper.revertInMs(), capacityBytes: Number.isFinite(this.shaper.capacity()) ? this.shaper.capacity() : null },
      budgetBps: this.budgetBps,
      edgeBudgetBps: this.edgeBudget(nowMs),
      link: this.linkView(nowMs, views),
      fusion: this.fusion.enabled,
      twinError: this.twinError(),
    };
  }
}
