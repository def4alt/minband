// Cursor on Target export: one CoT event per fused global entity, to ATAK/WinTAK/iTAK over UDP
// (unicast, broadcast or the SA multicast group) and to TAK Server's plain-CoT TCP input.
//
// Field choices (CoT 2.0 base schema, Event.xsd):
//  - type `a-u-G`: atom, affiliation unknown, ground track. Nothing more specific: a detected
//    person is a dismount, not an infantry unit (a-u-G-U-C-I would claim one); the class leads
//    the callsign instead ("DISMOUNT g3").
//  - how: Event.xsd defines `m-p` "predicted - prediction of future (e.g. from a tracker)" and
//    `m-f` "fused - corroborated from multiple sources". Every twin position is dead-reckoned from
//    the edge tracker's last update, so `m-p`; a group of >= 2 devices that is not coasting is
//    `m-f`. Not `m-g` (derived from GPS), which most feeds use by default and would overclaim.
//    The provenance state (S4: unconfirmed / two sensors / operator-seen) can refine this later.
//  - ce: the entity's `ce` (metres; the edge's declared threshold, grown with silence). Event.xsd
//    reads ce as a 1-sigma radius; ours is a bound, so it is conservative as one. le = ce when the
//    anchor altitude is known (the threshold bounds the 3D error, so also its vertical part),
//    else hae and le are 9999999 (CoT "unknown"). The anchor's own survey error is not included.
//  - time = start = now; stale = now for a stale entity, else now + validity (default
//    max(5 s, 3 send periods), so two lost UDP datagrams do not flicker the marker). When an
//    entity disappears (or the anchor is cleared) its last event is sent once more with
//    stale = now, so TAK greys it out instead of showing it live until the validity runs out.
//
// Sending is state-based like the rest of MinBand: no queue. A TCP endpoint that is down or
// backed up skips the round; the next round carries the current picture.
import dgram from 'node:dgram';
import net from 'node:net';
import { courseSpeed, markerToGeodetic, type GeoAnchor } from './geo.js';
import type { GlobalEntity } from './types.js';

export const COT_MULTICAST = 'udp://239.2.3.1:6969'; // ATAK situational awareness group
const UNKNOWN = 9999999;

/** Class ids the edge tracks (core/src/classes.rs, viewer CLASS_NAME): COCO's, person leads as
 *  dismount; from 100 up MinBand's own (drone footage, tools/footage). The type stays a-u-G for all
 *  of them: the class goes in the callsign and remarks, never into the CoT type. */
const LABEL: Record<number, string> = {
  0: 'dismount', 1: 'bicycle', 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck',
  24: 'backpack', 25: 'umbrella', 26: 'handbag', 28: 'suitcase', 39: 'bottle', 41: 'cup',
  56: 'chair', 57: 'couch', 58: 'plant', 59: 'bed', 60: 'table', 62: 'tv', 63: 'laptop', 67: 'phone', 73: 'book',
  100: 'mover', 101: 'armoured',
};
/** What the label rests on, for the remarks. */
const NOTE: Record<number, string> = {
  0: ' (COCO person)',
  100: ' (unclassified ground mover: motion only, no appearance class)',
  101: ' (armoured vehicle: tank/IFV/APC by appearance, unverified)',
};
export const classLabel = (c: number) => LABEL[c] ?? `class ${c}`;

export interface CotOptions {
  /** Send rate (default 1 Hz). */
  hz?: number;
  /** Validity window of a live event in seconds (default max(5, 3 / hz)). */
  staleS?: number;
  /** uid = `<uidPrefix>-<gid>` (default "minband"). */
  uidPrefix?: string;
}

export interface CotEvent {
  uid: string; type: string; how: string;
  time: number; start: number; stale: number;
  lat: number; lon: number; hae: number; ce: number; le: number;
  callsign: string; course: number; speed: number; remarks: string;
}

const validityMs = (o: CotOptions) => 1000 * (o.staleS ?? Math.max(5, 3 / (o.hz ?? 1)));
const finite = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x);

/** One event per fused entity, positions from the marker frame through the anchor. */
export function cotEvents(global: readonly GlobalEntity[], anchor: GeoAnchor, nowMs: number, o: CotOptions = {}): CotEvent[] {
  return global.map(g => {
    const p = markerToGeodetic(g.pos, anchor);
    const { course, speed } = courseSpeed(g.vel, anchor.headingDeg);
    const ce = finite(g.ce) && g.ce >= 0 ? Math.min(g.ce, UNKNOWN) : UNKNOWN; // ce is filled by fusion (S14)
    const altKnown = anchor.altM !== null;
    const coasting = g.coasting === true, n = g.sources.length;
    const label = classLabel(g.class);
    return {
      uid: `${o.uidPrefix ?? 'minband'}-${g.gid}`, type: 'a-u-G', how: n >= 2 && !coasting ? 'm-f' : 'm-p',
      time: nowMs, start: nowMs, stale: g.stale ? nowMs : nowMs + validityMs(o),
      lat: p.lat, lon: p.lon, hae: altKnown ? p.alt : UNKNOWN, ce, le: altKnown ? ce : UNKNOWN,
      callsign: `${label.toUpperCase()} ${g.gid}`, course, speed,
      remarks: `MinBand ${g.gid}: ${label}${NOTE[g.class] ?? ''}, ${n} source${n === 1 ? '' : 's'}`
        + `${coasting ? ', coasting' : ''}${g.stale ? ', stale' : ''}. `
        + (ce < UNKNOWN ? `ce ${ce.toFixed(2)} m is the twin's error bound (grows while the link is silent). ` : '')
        + 'Position dead-reckoned between edge updates; affiliation not assessed.',
    };
  });
}

const XML_ESC: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' };
/** Escape for XML text and attribute values; drops characters XML 1.0 cannot carry. */
export const xmlEscape = (s: string) =>
  s.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]/g, '').replace(/[&<>"']/g, c => XML_ESC[c]);
const iso = (ms: number) => new Date(ms).toISOString();
const num = (x: number, d: number) => (x >= UNKNOWN ? '9999999.0' : x.toFixed(d));

/** One CoT event document (XML declaration included, as pytak and ATAK send it). */
export function eventXml(e: CotEvent): string {
  const a = (k: string, v: string) => ` ${k}="${xmlEscape(v)}"`;
  return '<?xml version="1.0" encoding="UTF-8"?>\n'
    + `<event version="2.0"${a('uid', e.uid)}${a('type', e.type)}${a('how', e.how)}${a('time', iso(e.time))}${a('start', iso(e.start))}${a('stale', iso(e.stale))}>`
    + `<point lat="${e.lat.toFixed(7)}" lon="${e.lon.toFixed(7)}" hae="${num(e.hae, 2)}" ce="${num(e.ce, 2)}" le="${num(e.le, 2)}"/>`
    + `<detail><contact${a('callsign', e.callsign)}/><track course="${e.course.toFixed(1)}" speed="${e.speed.toFixed(2)}"/>`
    + `<remarks>${xmlEscape(e.remarks)}</remarks></detail></event>\n`;
}

/** Several events as one well-formed document (GET /api/cot). */
export function eventsXml(events: readonly CotEvent[]): string {
  return `<?xml version="1.0" encoding="UTF-8"?>\n<events count="${events.length}">\n`
    + events.map(e => eventXml(e).replace(/^<\?xml[^>]*\?>\n/, '')).join('') + '</events>\n';
}

// ---- endpoints ----------------------------------------------------------------------------------

export interface CotEndpointSpec { proto: 'udp' | 'tcp'; host: string; port: number; ttl?: number; iface?: string; url: string }

/** `udp://239.2.3.1:6969?ttl=2&iface=192.168.1.10,tcp://takserver:8087`; empty = none. */
export function parseCotEndpoints(s: string): CotEndpointSpec[] {
  return s.split(',').map(x => x.trim()).filter(Boolean).map(raw => {
    let u: URL;
    try { u = new URL(raw); } catch { throw new Error(`bad CoT endpoint "${raw}" (want udp://host:port or tcp://host:port)`); }
    const proto = u.protocol.slice(0, -1), port = Number(u.port), host = u.hostname.replace(/^\[|\]$/g, '');
    if (proto !== 'udp' && proto !== 'tcp') throw new Error(`CoT endpoint "${raw}": scheme must be udp or tcp (TLS/ssl is not supported)`);
    if (!host || !Number.isInteger(port) || port < 1 || port > 65535) throw new Error(`CoT endpoint "${raw}": needs host:port`);
    const ttl = u.searchParams.get('ttl'), iface = u.searchParams.get('iface');
    if (ttl !== null && !(Number.isInteger(Number(ttl)) && Number(ttl) >= 1 && Number(ttl) <= 255)) throw new Error(`CoT endpoint "${raw}": ttl must be 1..255`);
    return { proto, host, port, url: `${proto}://${u.host}`, ...(ttl !== null ? { ttl: Number(ttl) } : {}), ...(iface ? { iface } : {}) };
  });
}

export interface CotOut { readonly url: string; send(xml: string): boolean; close(): void; readonly connected: boolean }

const isMulticast4 = (h: string) => /^(22[4-9]|23\d)\./.test(h);

/** One datagram per event. Multicast TTL defaults to 1 (same Wi-Fi). */
export class UdpOut implements CotOut {
  private sock: dgram.Socket;
  readonly connected = true;
  constructor(readonly spec: CotEndpointSpec, private log: (s: string) => void = console.log) {
    this.sock = dgram.createSocket(net.isIPv6(spec.host) ? 'udp6' : 'udp4');
    this.sock.on('error', e => this.log(`cot: ${spec.url}: ${e.message}`));
    this.sock.bind(0, () => {
      try {
        this.sock.setBroadcast(true); // lets x.x.x.255 reach every TAK device on the subnet
        if (isMulticast4(spec.host)) {
          this.sock.setMulticastTTL(spec.ttl ?? 1);
          if (spec.iface) this.sock.setMulticastInterface(spec.iface);
        }
      } catch (e) { this.log(`cot: ${spec.url}: ${(e as Error).message}`); }
    });
  }
  get url() { return this.spec.url; }
  send(xml: string): boolean {
    this.sock.send(Buffer.from(xml, 'utf8'), this.spec.port, this.spec.host, e => { if (e) this.log(`cot: ${this.spec.url}: ${e.message}`); });
    return true;
  }
  close() { try { this.sock.close(); } catch { /* already closed */ } }
}

/** TAK Server plain CoT input (e.g. :8087): a stream of events; reconnects with backoff. */
export class TcpOut implements CotOut {
  private sock: net.Socket | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private backoff: number;
  private closed = false;
  connected = false;
  constructor(readonly spec: CotEndpointSpec, private log: (s: string) => void = console.log,
    private readonly backoffMs = { min: 1000, max: 30_000 }, private readonly maxQueuedBytes = 256 * 1024) {
    this.backoff = backoffMs.min;
    this.connect();
  }
  get url() { return this.spec.url; }
  private connect() {
    this.timer = null;
    const s = net.connect({ host: this.spec.host, port: this.spec.port });
    this.sock = s;
    s.setNoDelay(true); s.setKeepAlive(true, 10_000);
    s.on('connect', () => { this.connected = true; this.backoff = this.backoffMs.min; this.log(`cot: ${this.spec.url} connected`); });
    s.on('data', () => {}); // TAK Server may echo or ping; nothing to read
    s.on('error', e => { if (this.connected || this.backoff === this.backoffMs.min) this.log(`cot: ${this.spec.url}: ${e.message}`); });
    s.on('close', () => {
      const was = this.connected;
      this.connected = false; this.sock = null;
      if (this.closed) return;
      if (was) this.log(`cot: ${this.spec.url} disconnected`);
      this.timer = setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, this.backoffMs.max);
    });
  }
  /** False when not connected or the kernel buffer is backed up (the round is skipped, not queued). */
  send(xml: string): boolean {
    if (!this.connected || !this.sock || this.sock.writableLength > this.maxQueuedBytes) return false;
    this.sock.write(xml);
    return true;
  }
  close() {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.sock?.destroy();
  }
}

export function openEndpoint(spec: CotEndpointSpec, log?: (s: string) => void): CotOut {
  return spec.proto === 'udp' ? new UdpOut(spec, log) : new TcpOut(spec, log);
}

// ---- sender -------------------------------------------------------------------------------------

/** What the sender reads; World satisfies it. */
export interface CotSource {
  geo: GeoAnchor | null;
  lastSnapshot: { t: number; global: GlobalEntity[] } | null;
  snapshot(nowMs?: number): { t: number; global: GlobalEntity[] };
  readonly now: () => number;
}

/** Events for the current picture (the 30 Hz snapshot when fresh); empty without an anchor. */
export function currentCotEvents(src: CotSource, nowMs = src.now(), o: CotOptions = {}): CotEvent[] {
  if (!src.geo) return [];
  const s = src.lastSnapshot && nowMs - src.lastSnapshot.t < 1000 ? src.lastSnapshot : src.snapshot(nowMs);
  return cotEvents(s.global, src.geo, nowMs, o);
}

export class CotSender {
  /** Events of the last round by uid, to send a final stale event for each that disappears. */
  private last = new Map<string, CotEvent>();
  private interval: ReturnType<typeof setInterval> | null = null;
  private warnedNoAnchor = false;
  counters = { rounds: 0, events: 0, staleFinals: 0, skipped: 0 };

  constructor(private readonly src: CotSource, readonly outs: CotOut[], readonly opts: CotOptions = {}, private log: (s: string) => void = console.log) {}

  start(): this {
    const hz = this.opts.hz ?? 1;
    this.interval = setInterval(() => this.tick(), 1000 / hz);
    return this;
  }

  stop() {
    if (this.interval) clearInterval(this.interval);
    this.interval = null;
    for (const o of this.outs) o.close();
  }

  current(nowMs = this.src.now()): CotEvent[] { return currentCotEvents(this.src, nowMs, this.opts); }

  /** One round: current events plus a stale final for every uid that went away. */
  tick(nowMs = this.src.now()): CotEvent[] {
    if (!this.src.geo) {
      if (!this.warnedNoAnchor) this.log('cot: no geodetic anchor (MINBAND_GEO="lat,lon,headingDeg[,altM]" or GET /api/geo?lat=&lon=&heading=); not sending until one is set');
      this.warnedNoAnchor = true;
    } else if (this.warnedNoAnchor) { this.log('cot: anchor set, sending'); this.warnedNoAnchor = false; }
    const cur = this.current(nowMs);
    const out = [...cur];
    const ids = new Set(cur.map(e => e.uid));
    for (const [uid, e] of this.last) if (!ids.has(uid)) {
      out.push({ ...e, time: nowMs, start: nowMs, stale: nowMs, remarks: `${e.remarks} Track ended.` });
      this.counters.staleFinals++;
    }
    this.last = new Map(cur.map(e => [e.uid, e]));
    this.counters.rounds++;
    for (const e of out) {
      const xml = eventXml(e);
      for (const o of this.outs) if (o.send(xml)) this.counters.events++; else this.counters.skipped++;
    }
    return out;
  }
}

export interface CotConfig { endpoints: CotEndpointSpec[]; opts: CotOptions }

/** MINBAND_COT, MINBAND_COT_HZ, MINBAND_COT_STALE_S. Throws on bad values. */
export function cotConfigFromEnv(env: NodeJS.ProcessEnv): CotConfig {
  const endpoints = parseCotEndpoints(env.MINBAND_COT ?? '');
  const hz = env.MINBAND_COT_HZ ? Number(env.MINBAND_COT_HZ) : 1;
  if (!(hz > 0 && hz <= 30)) throw new Error(`MINBAND_COT_HZ must be in (0, 30], got "${env.MINBAND_COT_HZ}"`);
  const opts: CotOptions = { hz };
  if (env.MINBAND_COT_STALE_S) {
    const s = Number(env.MINBAND_COT_STALE_S);
    if (!(s > 0 && s <= 3600)) throw new Error(`MINBAND_COT_STALE_S must be in (0, 3600], got "${env.MINBAND_COT_STALE_S}"`);
    opts.staleS = s;
  }
  return { endpoints, opts };
}

/** Opens the endpoints and starts sending; null (with a log line) when none are configured. */
export function startCot(src: CotSource, cfg: CotConfig, log: (s: string) => void = console.log): CotSender | null {
  if (!cfg.endpoints.length) { log(`cot: disabled (set MINBAND_COT, e.g. ${COT_MULTICAST})`); return null; }
  const outs = cfg.endpoints.map(e => openEndpoint(e, log));
  log(`cot: ${outs.map(o => o.url).join(', ')} at ${cfg.opts.hz ?? 1} Hz`);
  const s = new CotSender(src, outs, cfg.opts, log);
  s.tick(); // logs at once when there is no anchor yet
  return s.start();
}
