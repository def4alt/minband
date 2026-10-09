export const TICK_HZ = 120;

export interface EntityView {
  id: number; class: number; pos: [number, number, number]; vel: [number, number, number];
  conf: number; tick: number; age: number; stale: boolean;
  // Added for the hackathon P1 (S14/S15), produced by core's `extrapolate_json`:
  /** Position threshold (m) the edge declared (theta_q) in the datagram that last refreshed this entity. */
  theta: number;
  /** The device missed its heartbeat (no datagram for one keyframe period): the entity is coasting. */
  coasting: boolean;
  /** Honest error radius (m): `theta` while the heartbeat holds, then grows with coast time x class max speed. */
  ce: number;
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
  // Added for the hackathon P0/P1 (S19, S15, S2):
  /** Heartbeat cadence the edge derives from the current budget (core `cadence_json`), in ms. */
  cadence: Cadence;
  /** No datagram for one keyframe period (plus margin): every entity is coasting. */
  coasting: boolean;
  /** Share of channel time this device's uplink used over the rate window, under the current link model (0 when no model). */
  airtimeShare: number;
}
/** Heartbeat periods derived from the edge budget by core (same function on both ends). */
export interface Cadence { keyframeMs: number; helloRefreshMs: number; poseMs: number; coastMs: number; staleMs: number; dropMs: number }
export interface GeoPoint { lat: number; lon: number; mgrs: string }
export interface GlobalEntity {
  gid: string; class: number; pos: [number, number, number]; vel: [number, number, number];
  sources: { deviceId: number; id: number }[]; stale: boolean;
  // Added for the hackathon P1:
  /** Honest error radius (m) of the fused entity (S14/S15; the best source's `ce`). */
  ce: number;
  /** Every source is coasting. */
  coasting: boolean;
  /** WGS84 + MGRS of `pos` when a geodetic anchor is configured (S3), else null. */
  geo: GeoPoint | null;
}
/** Airtime model of the link, for time-on-air (S2). */
export type AirtimeModel =
  | { kind: 'none' }
  /** Serial-class radio (HF, ELRS/SiK telemetry): every payload byte costs `bitsPerByte` bits at `rateBps`, plus `overheadBytes` framing per datagram. */
  | { kind: 'serial'; rateBps: number; bitsPerByte: number; overheadBytes: number }
  /** LoRa (Semtech time-on-air formula); defaults = Meshtastic LongFast (SF11, 250 kHz, CR 4/5, 16-symbol preamble). */
  | { kind: 'lora'; sf: number; bwHz: number; cr: number; preamble: number; crc: boolean; explicitHeader: boolean; lowDataRateOptimize: boolean; overheadBytes: number };
/** A named link profile; the same table as the Pi link box (docs/HACKATHON_PLAN.md section 3.3). */
export interface LinkProfile {
  name: string; bps: number; delayMs: number; loss: number; queue: number;
  /** Edge budget to set with this profile (0 = unlimited). */
  budgetBps: number;
  airtime: AirtimeModel;
  /** "Stands for" column, shown in the viewer. */
  label: string;
}
export interface LinkView {
  /** Active profile name, 'custom' when the shaper was set by hand, 'external' when the Pi box shapes. */
  profile: string;
  model: AirtimeModel;
  /** Uplink channel time used per second of wall time over the rate window (0..1, can exceed 1 when oversubscribed). */
  airtimeShare: number;
  /** Uplink datagrams/s over the rate window, all devices. */
  msgsPerSec: number;
  profiles: LinkProfile[];
  // Optional additions (server, hackathon S2):
  /** With profile 'external': the profile whose budget and airtime model are used. */
  as?: string;
  /** Downlink (acks) channel time per second under the same model; a half-duplex radio shares the channel with the uplink. */
  downAirtimeShare?: number;
  /** Acks/s over the rate window, all devices. */
  downMsgsPerSec?: number;
  /** Only while profile is 'contested': current phase and ms until it switches. */
  contested?: { blackout: boolean; switchInMs: number };
}
/** One datagram, for the packet waterfall (V3). Sent in the snapshot that follows it. */
export interface PacketEvent {
  /** Server wall time (ms) the datagram arrived at the socket (up) or was sent (down). */
  t: number;
  dir: 'up' | 'down';
  /** Device render key (`id:<device_id>` / `addr:<ip:port>`), '' if unattributed. */
  key: string;
  kind: 'hello' | 'delta' | 'keyframe' | 'pose' | 'bye' | 'ack' | 'malformed';
  /** Bytes on the link, including the 28 B UDP/IPv4 header. */
  bytes: number;
  seq?: number;
  /** Entity ids carried (delta, keyframe). */
  ids?: number[];
  /** Dropped by the in-process shaper (loss or cap). */
  dropped: boolean;
}
/** H.264 reference bitrates (Baseline A): measured when runs/baseline_a.json exists, else configured. */
export interface BaselineAEntry { id: string; label: string; bps: number; measured: boolean; source: string }
export interface ShaperConfig {
  bps: number; delayMs: number; loss: number; enabled: boolean;
  /** Token bucket depth in seconds of `bps` (added in M5, default 0.5). */
  burstSec: number;
  /** Max datagrams held in the delay line, like netem's `limit` (hackathon S2, optional; 0 or absent = unbounded). */
  queue?: number;
}
export interface Snapshot {
  t: number; devices: DeviceView[]; global: GlobalEntity[]; shaper: ShaperConfig; fusion: boolean;
  baselines: { h264_720p_bps: number; h264_480p_bps: number; naiveMetadataBps: number };
  // Added in M5 (additive):
  budgetBps: number;
  /** Milliseconds until a timed shaper override (e.g. blackout) reverts, else null. */
  shaperRevertMs: number | null;
  // Added for the hackathon P0/P1:
  /** Link profile and time-on-air (S2). */
  link: LinkView;
  /** Datagrams since the previous snapshot (V3). */
  packets: PacketEvent[];
  /** H.264 table (720p/480p/360p); `measured` false until runs/baseline_a.json exists. */
  baselineA: BaselineAEntry[];
  /** Geodetic anchor of the marker origin (S3), null when not configured. */
  geo: { lat: number; lon: number; headingDeg: number; mgrs: string } | null;
}
export type ControlMessage =
  | { type: 'shaper'; config: Partial<ShaperConfig>; /** M5: apply for this long, then restore. */ revertAfterMs?: number }
  | { type: 'budget'; bps: number }
  | { type: 'fusion'; enabled: boolean }
  /** Apply a named link profile (shaper + budget + airtime model); 'external' = shaper off, airtime model of `as`. */
  | { type: 'link'; profile: string; as?: string };

export interface TwinError { meanM: number | null; p95M: number | null; samples: number }
