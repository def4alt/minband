export const TICK_HZ = 120;

export interface EntityView {
  id: number; class: number; pos: [number, number, number]; vel: [number, number, number];
  conf: number; tick: number; age: number; stale: boolean;
}
export interface PoseView { pos: [number, number, number]; quat: [number, number, number, number]; originLocked: boolean; tick: number }
export interface DeviceView {
  deviceId: number; addr: string; entities: EntityView[]; pose: PoseView | null;
  bps: number; msgsPerSec: number; stats: Record<string, number>; lastSeenMs: number;
  // Added in M5 (additive):
  /** Stable render key: `id:<device_id>` once Hello was seen, `addr:<ip:port>` before (provisional). */
  key: string;
  /** No Hello seen yet: deviceId is 0 and the device is keyed by its UDP address. */
  provisional: boolean;
  /** Bytes/s (as bits/s) arriving at the socket before the shaper; `bps` is what got through. */
  offeredBps: number;
  /** Edge tick the entities were extrapolated to (receiver's estimate of edge now). */
  edgeTick: number;
  /** No datagram for 5 s: every entity is reported stale. */
  silent: boolean;
  /** Times this device moved to a new UDP address. */
  addrChanges: number;
  /** Estimated localMs - edgeMs, null before the first datagram. */
  clockOffsetMs: number | null;
}
export interface GlobalEntity {
  gid: string; class: number; pos: [number, number, number]; vel: [number, number, number];
  sources: { deviceId: number; id: number }[]; stale: boolean;
}
export interface ShaperConfig {
  bps: number; delayMs: number; loss: number; enabled: boolean;
  /** Token bucket depth in seconds of `bps` (added in M5, default 0.5). */
  burstSec: number;
}
export interface Snapshot {
  t: number; devices: DeviceView[]; global: GlobalEntity[]; shaper: ShaperConfig; fusion: boolean;
  baselines: { h264_720p_bps: number; h264_480p_bps: number; naiveMetadataBps: number };
  // Added in M5 (additive):
  budgetBps: number;
  /** Milliseconds until a timed shaper override (e.g. blackout) reverts, else null. */
  shaperRevertMs: number | null;
}
export type ControlMessage =
  | { type: 'shaper'; config: Partial<ShaperConfig>; /** M5: apply for this long, then restore. */ revertAfterMs?: number }
  | { type: 'budget'; bps: number }
  | { type: 'fusion'; enabled: boolean };

export interface TwinError { meanM: number | null; p95M: number | null; samples: number }
