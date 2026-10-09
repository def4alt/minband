// World model: one WASM Receiver per device, clock offset estimation, metrics, fusion.
import { WasmReceiver } from 'minband-core';
import { Fusion } from './fusion.js';
import { Shaper } from './shaper.js';
import type { DeviceView, EntityView, PoseView, Snapshot } from './types.js';
import { TICK_HZ } from './types.js';

const DEVICE_TIMEOUT_MS = 30_000;
const BASELINES = { h264_720p_bps: 1_500_000, h264_480p_bps: 500_000, naiveMetadataBps: 0 };

class Device {
  rx = new WasmReceiver();
  // edge tick <-> local ms: offset = min over recent samples of (localMs - tickMs), so
  // extrapolation targets "edge now" without the receiver's queueing delay.
  private offsets: number[] = [];
  bytesWindow: { t: number; b: number }[] = [];
  lastSeenMs = 0;
  lastAckMs = 0;
  constructor(public deviceId: number, public addr: string) {}

  ingest(buf: Uint8Array, nowMs: number): string {
    const ev = this.rx.on_datagram(buf);
    this.lastSeenMs = nowMs;
    this.bytesWindow.push({ t: nowMs, b: buf.length + 28 });
    const tickMs = this.rx.last_edge_tick() / TICK_HZ * 1000;
    this.offsets.push(nowMs - tickMs); if (this.offsets.length > 50) this.offsets.shift();
    return ev;
  }
  edgeTickNow(nowMs: number): number {
    if (!this.offsets.length) return this.rx.last_edge_tick();
    const off = Math.min(...this.offsets);
    return Math.max(0, Math.round((nowMs - off) / 1000 * TICK_HZ));
  }
  rate(nowMs: number): { bps: number; msgsPerSec: number } {
    const w = 2000; this.bytesWindow = this.bytesWindow.filter(x => nowMs - x.t < w);
    return { bps: this.bytesWindow.reduce((a, x) => a + x.b, 0) * 8 / (w / 1000), msgsPerSec: this.bytesWindow.length / (w / 1000) };
  }
  view(nowMs: number): DeviceView {
    const tick = this.edgeTickNow(nowMs);
    this.rx.gc(tick);
    const entities = JSON.parse(this.rx.extrapolate_json(tick)) as EntityView[];
    const poseJson = this.rx.pose_json();
    const pose = poseJson ? (JSON.parse(poseJson) as PoseView) : null;
    return { deviceId: this.deviceId, addr: this.addr, entities, pose, ...this.rate(nowMs), stats: JSON.parse(this.rx.stats_json()), lastSeenMs: this.lastSeenMs };
  }
}

export class World {
  devices = new Map<string, Device>(); // keyed by addr
  fusion = new Fusion();
  shaper = new Shaper();
  budgetBps = 0;
  log: string[] = [];

  /** Called for every raw datagram from the socket. Returns an ack to send back, if due. */
  ingest(addr: string, buf: Uint8Array, nowMs: number): Uint8Array | null {
    let dev = this.devices.get(addr);
    if (!dev) { dev = new Device(0, addr); this.devices.set(addr, dev); }
    const d = dev;
    this.shaper.offer(buf.length, () => {
      try {
        const ev = d.ingest(buf, nowMs);
        const id = d.rx.device_id(); if (id !== undefined) d.deviceId = id;
        if (this.log.length > 200) this.log.shift();
        this.log.push(`${new Date(nowMs).toISOString().slice(11, 23)} ${addr} ${ev}`);
      } catch (e) { this.log.push(`${addr} malformed: ${e}`); }
    });
    if (d.rx.needs_ack() || nowMs - d.lastAckMs >= 100) { d.lastAckMs = nowMs; return d.rx.make_ack(this.budgetBps); }
    return null;
  }

  snapshot(nowMs: number): Snapshot {
    for (const [addr, d] of this.devices) if (nowMs - d.lastSeenMs > DEVICE_TIMEOUT_MS) this.devices.delete(addr);
    const devices = [...this.devices.values()].map(d => d.view(nowMs));
    const global = this.fusion.update(devices, nowMs);
    const entityCount = devices.reduce((a, d) => a + d.entities.length, 0);
    return {
      t: nowMs, devices, global, shaper: this.shaper.config, fusion: this.fusion.enabled,
      baselines: { ...BASELINES, naiveMetadataBps: entityCount * 31 * 30 * 8 + 30 * 40 * 8 },
    };
  }
}
