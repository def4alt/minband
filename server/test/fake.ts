// Test helpers: a fake clock with timers, and a scripted edge that drives a World.
import { WasmEdge } from 'minband-core';
import type { World } from '../src/world.js';
import { TICK_HZ } from '../src/types.js';

export class FakeClock {
  t: number;
  private timers: { at: number; id: number; fn: () => void }[] = [];
  private nextId = 1;
  constructor(t0 = 1_000_000) { this.t = t0; }
  now = (): number => this.t;
  schedule = (fn: () => void, ms: number): number => {
    const id = this.nextId++;
    this.timers.push({ at: this.t + Math.max(0, ms), id, fn });
    return id;
  };
  cancel = (h: unknown): void => { this.timers = this.timers.filter(x => x.id !== h); };
  /** Advance time, firing due timers in (time, creation) order. */
  advance(ms: number): void {
    const end = this.t + ms;
    for (;;) {
      this.timers.sort((a, b) => a.at - b.at || a.id - b.id);
      const n = this.timers[0];
      if (!n || n.at > end) break;
      this.timers.shift(); this.t = Math.max(this.t, n.at); n.fn();
    }
    this.t = end;
  }
  get pending(): number { return this.timers.length; }
}

export function unpack(buf: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = []; let i = 0;
  while (i + 2 <= buf.length) { const n = buf[i] | (buf[i + 1] << 8); i += 2; out.push(buf.slice(i, i + n)); i += n; }
  return out;
}

export interface Track { id: number; class: number; pos: number[]; vel: number[]; conf: number }

/** One walker on a circle plus a static chair, as seen by the edge at tick `tick`. */
export function scene(tick: number): Track[] {
  const t = tick / TICK_HZ;
  return [
    { id: 1, class: 0, pos: [2 * Math.cos(t * 0.5), 0, 2 * Math.sin(t * 0.5)], vel: [-1 * Math.sin(t * 0.5), 0, 1 * Math.cos(t * 0.5)], conf: 220 },
    { id: 2, class: 56, pos: [-1, 0, 1], vel: [0, 0, 0], conf: 180 },
  ];
}

/**
 * A WasmEdge wired to a World through a fake clock. `addr` is where the phone currently sends
 * from; acks addressed elsewhere are lost (like acks to a port the phone no longer listens on).
 */
export class ScriptedEdge {
  edge: WasmEdge;
  tick = 0;
  sent = 0;
  constructor(readonly world: World, readonly clock: FakeClock, public addr: string, readonly deviceId: number, nonce: number, readonly tracks: (tick: number) => Track[] = scene) {
    this.edge = new WasmEdge(deviceId, nonce);
  }
  /** Feed an ack the World produced (call from world.onAck). */
  onAck(addr: string, ack: Uint8Array) { if (addr === this.addr) this.edge.on_datagram(ack); }
  /** Run `ticks` edge ticks, advancing the clock; `deliver=false` emulates a dead link. */
  run(ticks: number, opts: { deliver?: boolean; snapshotEvery?: number } = {}) {
    const deliver = opts.deliver ?? true;
    for (let i = 0; i < ticks; i++) {
      for (const dg of unpack(this.edge.tick(JSON.stringify(this.tracks(this.tick)), this.tick))) {
        if (deliver) { this.world.ingest(this.addr, dg); this.sent++; }
      }
      this.tick++;
      this.clock.advance(1000 / TICK_HZ);
      if (opts.snapshotEvery && this.tick % opts.snapshotEvery === 0) this.world.snapshot();
    }
  }
  free() { this.edge.free(); }
}

/** Route World acks to whichever scripted edges are listening. */
export function wireAcks(world: World, edges: ScriptedEdge[]): { addr: string }[] {
  const log: { addr: string }[] = [];
  world.onAck = (addr, ack) => { log.push({ addr }); for (const e of edges) e.onAck(addr, ack); };
  return log;
}

/** A track that jumps 0.5 m every tick, so the edge sends a Delta on every tick. */
export function chatty(tick: number): Track[] {
  return [{ id: 1, class: 0, pos: [0.5 * (tick % 2), 0, 0], vel: [0, 0, 0], conf: 200 }];
}
