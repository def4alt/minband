// Process harness for the e2e tests: the real server (WASM core inside), sim edges sending real
// UDP, the Vite viewer, and a WebSocket snapshot feed. Every process gets free ports so a stale
// dev server on 7777/8080 does not interfere, and everything is killed on stop or exit.
import { spawn, type ChildProcess } from 'node:child_process';
import { createServer } from 'node:net';
import dgram from 'node:dgram';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import WebSocket from 'ws';

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SERVER_DIR = join(ROOT, 'server');
const VIEWER_DIR = join(ROOT, 'viewer');
const TSX = join(SERVER_DIR, 'node_modules', '.bin', 'tsx');
const VITE = join(VIEWER_DIR, 'node_modules', '.bin', 'vite');

const live = new Set<ChildProcess>();
process.on('exit', () => { for (const p of live) p.kill('SIGKILL'); });

export const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

export async function freeTcpPort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = createServer(); s.unref(); s.on('error', reject);
    s.listen(0, '127.0.0.1', () => { const p = (s.address() as { port: number }).port; s.close(() => resolve(p)); });
  });
}
export async function freeUdpPort(): Promise<number> {
  return new Promise(resolve => {
    const s = dgram.createSocket('udp4');
    s.bind(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

/** Poll `fn` until it returns a truthy value or the timeout passes. */
export async function waitFor<T>(what: string, fn: () => T | Promise<T>, timeoutMs = 10_000, everyMs = 50): Promise<NonNullable<T>> {
  const end = Date.now() + timeoutMs; let last: unknown;
  for (;;) {
    try { const v = await fn(); if (v) return v as NonNullable<T>; } catch (e) { last = e; }
    if (Date.now() > end) throw new Error(`timed out after ${timeoutMs} ms waiting for ${what}${last ? `: ${last}` : ''}`);
    await sleep(everyMs);
  }
}

export class Proc {
  out = '';
  readonly child: ChildProcess;
  readonly name: string;
  constructor(name: string, cmd: string, args: string[], opts: { cwd: string; env?: Record<string, string> }) {
    this.name = name;
    this.child = spawn(cmd, args, { cwd: opts.cwd, env: { ...process.env, ...opts.env }, stdio: ['ignore', 'pipe', 'pipe'] });
    live.add(this.child);
    const keep = (b: Buffer) => { this.out += b.toString(); if (this.out.length > 200_000) this.out = this.out.slice(-100_000); };
    this.child.stdout!.on('data', keep); this.child.stderr!.on('data', keep);
    this.child.on('exit', () => live.delete(this.child));
  }
  get exited() { return this.child.exitCode !== null || this.child.signalCode !== null; }
  async stop() {
    if (this.exited) return;
    const done = new Promise(r => this.child.once('exit', r));
    this.child.kill('SIGTERM');
    await Promise.race([done, sleep(3000)]);
    if (!this.exited) this.child.kill('SIGKILL');
  }
}

export interface ServerHandle { proc: Proc; udpPort: number; wsPort: number; api: string; ws: string; stop(): Promise<void> }

export async function startServer(env: Record<string, string> = {}): Promise<ServerHandle> {
  const udpPort = await freeUdpPort(), wsPort = await freeTcpPort();
  const proc = new Proc('server', TSX, ['src/main.ts'], {
    cwd: SERVER_DIR,
    env: { MINBAND_UDP_PORT: String(udpPort), MINBAND_WS_PORT: String(wsPort), ...env },
  });
  const api = `http://127.0.0.1:${wsPort}`;
  await waitFor('server http', async () => { if (proc.exited) throw new Error(`server exited:\n${proc.out}`); return (await fetch(`${api}/api/metrics`)).ok; }, 20_000, 100);
  return { proc, udpPort, wsPort, api, ws: `ws://127.0.0.1:${wsPort}`, stop: () => proc.stop() };
}

export function startSim(server: ServerHandle, env: Record<string, string> = {}): Proc {
  return new Proc('sim', TSX, ['src/sim.ts'], {
    cwd: SERVER_DIR,
    env: { MINBAND_HOST: '127.0.0.1', MINBAND_UDP_PORT: String(server.udpPort), MINBAND_WS_PORT: String(server.wsPort), ...env },
  });
}

export async function startViewer(server: ServerHandle): Promise<{ proc: Proc; url: string; stop(): Promise<void> }> {
  const port = await freeTcpPort();
  const proc = new Proc('viewer', VITE, ['--port', String(port), '--strictPort', '--host', '127.0.0.1'], {
    cwd: VIEWER_DIR, env: { VITE_WS_URL: server.ws, VITE_API_URL: server.api },
  });
  const url = `http://127.0.0.1:${port}/`;
  await waitFor('vite', async () => { if (proc.exited) throw new Error(`vite exited:\n${proc.out}`); return (await fetch(url)).ok; }, 30_000, 200);
  return { proc, url, stop: () => proc.stop() };
}

export async function getJson<T = any>(url: string): Promise<T> {
  const r = await fetch(url);
  const j = await r.json();
  if (!r.ok) throw new Error(`${url}: ${r.status} ${JSON.stringify(j)}`);
  return j as T;
}

/** WebSocket client that keeps the latest snapshot and everything per-datagram since it connected. */
export class Feed {
  latest: any = null;
  snapshots = 0;
  packets: any[] = [];
  logLines: string[] = [];
  private ws: WebSocket;
  private history: { t: number; snap: any }[] = [];
  readonly keepHistoryMs: number;
  constructor(url: string, keepHistoryMs = 0) {
    this.keepHistoryMs = keepHistoryMs;
    this.ws = new WebSocket(url);
    this.ws.on('message', raw => {
      const m = JSON.parse(raw.toString());
      if (m.type === 'snapshot') {
        this.latest = m.snap; this.snapshots++;
        if (Array.isArray(m.snap.packets)) this.packets.push(...m.snap.packets);
        if (this.keepHistoryMs) {
          this.history.push({ t: Date.now(), snap: m.snap });
          while (this.history.length && Date.now() - this.history[0].t > this.keepHistoryMs) this.history.shift();
        }
      } else if (m.type === 'log') this.logLines.push(...m.lines);
    });
  }
  open() { return waitFor('ws open', () => this.ws.readyState === WebSocket.OPEN, 5000); }
  send(m: unknown) { this.ws.send(JSON.stringify(m)); }
  /** Snapshots received in the last `ms` (needs keepHistoryMs). */
  recent(ms: number) { const t = Date.now() - ms; return this.history.filter(h => h.t >= t).map(h => h.snap); }
  until<T>(what: string, pred: (snap: any) => T, timeoutMs = 10_000) { return waitFor(what, () => this.latest && pred(this.latest), timeoutMs); }
  close() { this.ws.close(); }
}

/** Server + sim + feed, the common fixture. */
export async function startStack(opts: { server?: Record<string, string>; sim?: Record<string, string> | null; historyMs?: number } = {}) {
  const server = await startServer(opts.server);
  const sim = opts.sim === null ? null : startSim(server, { GT_POST_MS: '2000', ...opts.sim });
  const feed = new Feed(server.ws, opts.historyMs ?? 0);
  await feed.open();
  return {
    server, sim, feed,
    async stop() { feed.close(); await sim?.stop(); await server.stop(); },
    logs: () => `--- server ---\n${server.proc.out.slice(-4000)}\n--- sim ---\n${sim?.out.slice(-4000) ?? ''}`,
  };
}
export type Stack = Awaited<ReturnType<typeof startStack>>;
