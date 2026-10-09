// Identify a datagram before choosing which per-device Receiver gets it. Decoding stays in Rust:
// this reads core's `describe()` string, e.g.
//   "Hello { device_id: 7, session_nonce: 123, caps: 0, tick: 5 }"
//   "Delta seq=12 tick=3456 updates=1", "Keyframe seq=3 tick=240 part=0/1 entities=4",
//   "Pose { seq: 5, tick: 600, ... }", "Bye { seq: 9, tick: 700 }", "error Malformed".
// TODO(core): expose a structured `peek_json` from the WASM build and drop the string parsing.
import { describe } from 'minband-core';

export type PeekKind = 'hello' | 'delta' | 'keyframe' | 'pose' | 'bye' | 'ack' | 'malformed';
export interface Peek { kind: PeekKind; deviceId?: number; nonce?: number; seq?: number; tick?: number; text: string }

const KINDS: [string, PeekKind][] = [['Hello', 'hello'], ['Delta', 'delta'], ['Keyframe', 'keyframe'], ['Pose', 'pose'], ['Bye', 'bye'], ['Ack', 'ack']];
const field = (s: string, name: string): number | undefined => {
  const m = new RegExp(`\\b${name}(?:: |=)(\\d+)`).exec(s);
  return m ? Number(m[1]) : undefined;
};

export function peek(buf: Uint8Array): Peek {
  const text = describe(buf);
  const kind = KINDS.find(([p]) => text.startsWith(p))?.[1] ?? 'malformed';
  const p: Peek = { kind, text, seq: field(text, 'seq'), tick: field(text, 'tick') };
  if (kind === 'hello') {
    p.deviceId = field(text, 'device_id'); p.nonce = field(text, 'session_nonce');
    if (p.deviceId === undefined || p.nonce === undefined) p.kind = 'malformed';
  }
  return p;
}
