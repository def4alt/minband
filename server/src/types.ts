export const TICK_HZ = 120;

export interface EntityView {
  id: number; class: number; pos: [number, number, number]; vel: [number, number, number];
  conf: number; tick: number; age: number; stale: boolean;
}
export interface PoseView { pos: [number, number, number]; quat: [number, number, number, number]; originLocked: boolean; tick: number }
export interface DeviceView {
  deviceId: number; addr: string; entities: EntityView[]; pose: PoseView | null;
  bps: number; msgsPerSec: number; stats: Record<string, number>; lastSeenMs: number;
}
export interface GlobalEntity {
  gid: string; class: number; pos: [number, number, number]; vel: [number, number, number];
  sources: { deviceId: number; id: number }[]; stale: boolean;
}
export interface ShaperConfig { bps: number; delayMs: number; loss: number; enabled: boolean }
export interface Snapshot {
  t: number; devices: DeviceView[]; global: GlobalEntity[]; shaper: ShaperConfig; fusion: boolean;
  baselines: { h264_720p_bps: number; h264_480p_bps: number; naiveMetadataBps: number };
}
export type ControlMessage =
  | { type: 'shaper'; config: Partial<ShaperConfig> }
  | { type: 'budget'; bps: number }
  | { type: 'fusion'; enabled: boolean };
