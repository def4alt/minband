// HTTP API on the same server the WebSocket attaches to (:8080). See server/README.md.
import type { IncomingMessage, ServerResponse } from 'node:http';
import { sanitizeShaper } from './shaper.js';
import type { World } from './world.js';

export const MAX_GT_BYTES = 64 * 1024 * 1024;

const ENDPOINTS = [
  'GET  /api/metrics',
  'GET  /api/shaper?enabled=0|1&bps=&delayMs=&loss=0..1&burstSec=&queue=&revertAfterMs=',
  'GET  /api/budget?bps=',
  'GET  /api/fusion?enabled=0|1',
  'POST /api/ground-truth?deviceId=  (body: CSV tick,id,class,x,y,z,vx,vy,vz,conf)',
];

const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'GET, POST, OPTIONS',
  'access-control-allow-headers': 'content-type',
};

function send(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { ...CORS, 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  res.end(JSON.stringify(body, null, 2));
}

function readBody(req: IncomingMessage, limit: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []; let n = 0;
    req.on('data', (c: Buffer) => { n += c.length; if (n <= limit) chunks.push(c); }); // past the limit: drain, then 413
    req.on('end', () => n > limit
      ? reject(Object.assign(new Error(`body over ${limit} bytes`), { status: 413 }))
      : resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

export function shaperState(world: World) {
  return {
    config: world.shaper.config,
    counters: { ...world.shaper.counters },
    revertInMs: world.shaper.revertInMs(),
  };
}

/** Request handler for `http.createServer`. */
export function createApi(world: World) {
  return async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const url = new URL(req.url ?? '/', 'http://localhost');
    const q = Object.fromEntries(url.searchParams);
    try {
      if (req.method === 'OPTIONS') { res.writeHead(204, CORS); res.end(); return; }
      switch (url.pathname) {
        case '/api/metrics':
          send(res, 200, world.metrics());
          return;
        case '/api/shaper': {
          const { revertAfterMs, ...rest } = q;
          const { ok, errors } = sanitizeShaper(rest);
          const unknown = Object.keys(rest).filter(k => !['bps', 'delayMs', 'loss', 'burstSec', 'queue', 'enabled'].includes(k));
          if (unknown.length) errors.push(`unknown parameter(s): ${unknown.join(', ')}`);
          let revert: number | undefined;
          if (revertAfterMs !== undefined) {
            revert = Number(revertAfterMs);
            if (!Number.isFinite(revert) || revert <= 0 || revert > 3_600_000) errors.push('revertAfterMs must be in (0, 3600000]');
          }
          if (errors.length) { send(res, 400, { error: errors.join('; ') }); return; }
          if (Object.keys(ok).length) {
            if (revert !== undefined) world.shaper.setFor(ok, revert); else world.shaper.set(ok);
          }
          send(res, 200, shaperState(world));
          return;
        }
        case '/api/budget': {
          if (q.bps !== undefined) {
            const b = Number(q.bps);
            if (!Number.isFinite(b) || b < 0 || b > 0xFFFFFFFF) { send(res, 400, { error: 'bps must be in [0, 2^32)' }); return; }
            world.budgetBps = Math.round(b);
          }
          send(res, 200, { budgetBps: world.budgetBps });
          return;
        }
        case '/api/fusion': {
          if (q.enabled !== undefined) {
            if (!['0', '1', 'true', 'false', 'on', 'off'].includes(q.enabled)) { send(res, 400, { error: 'enabled must be 0/1' }); return; }
            world.fusion.enabled = ['1', 'true', 'on'].includes(q.enabled);
          }
          send(res, 200, { fusion: world.fusion.enabled });
          return;
        }
        case '/api/ground-truth': {
          if (req.method !== 'POST') { send(res, 405, { error: 'POST a CSV body' }); return; }
          const id = Number(q.deviceId);
          if (q.deviceId === undefined || !Number.isInteger(id) || id < 0) { send(res, 400, { error: 'deviceId (u32) required' }); return; }
          const csv = await readBody(req, MAX_GT_BYTES);
          const r = world.postGroundTruth(id, csv);
          if (!r) { send(res, 404, { error: `no snapshots recorded for device ${id}` }); return; }
          send(res, 200, r);
          return;
        }
        default:
          send(res, 404, { error: 'not found', endpoints: ENDPOINTS, websocket: 'ws://<host>:8080 (snapshot 30 Hz, log 2 Hz)' });
      }
    } catch (e) {
      const status = (e as { status?: number }).status ?? 500;
      if (!res.headersSent) send(res, status, { error: String((e as Error).message ?? e) });
    }
  };
}
