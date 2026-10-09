// Identify a datagram before choosing which per-device Receiver gets it. Decoding stays in Rust:
// core's `peek_json()` returns e.g.
//   {"kind":"hello","deviceId":7,"nonce":123,"tick":5}
//   {"kind":"keyframe","seq":3,"tick":252,"part":0,"of":2,"ids":[4,9],"thetaM":0.15}
//   {"kind":"malformed","error":"BadVersion(0)"}
// `tick` is the edge tick the datagram was sent at (for a paced keyframe part, its newest entity
// tick). `text` keeps the human-readable `describe()` line for logs.
import { describe, peek_json } from 'minband-core';

export type PeekKind = 'hello' | 'delta' | 'keyframe' | 'pose' | 'bye' | 'ack' | 'malformed';
export interface Peek {
  kind: PeekKind; deviceId?: number; nonce?: number; seq?: number; tick?: number; text: string;
  /** Entity ids carried by a delta (spawns, updates, despawns) or a keyframe part. */
  ids?: number[];
  /** Keyframe part index and part count. */
  part?: number; of?: number;
  /** Position threshold (m) the edge declared in a delta or keyframe. */
  thetaM?: number;
}

export function peek(buf: Uint8Array): Peek {
  const { error: _error, ...j } = JSON.parse(peek_json(buf)) as Omit<Peek, 'text'> & { error?: string };
  return { ...j, text: describe(buf) };
}
