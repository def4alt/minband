//! wasm-bindgen surface for Node and the browser. Tracks come in as JSON (cheap at our rates);
//! datagrams go out as one byte buffer with u16-LE length prefixes to avoid Vec<Vec<u8>>.

use crate::edge::{Edge, EdgeConfig, Track};
use crate::receiver::{extrapolated_json, Receiver, ReceiverConfig};
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub struct WasmEdge { inner: Edge }

#[wasm_bindgen]
impl WasmEdge {
    #[wasm_bindgen(constructor)]
    pub fn new(device_id: u32, session_nonce: u32) -> WasmEdge {
        WasmEdge { inner: Edge::new(device_id, session_nonce, EdgeConfig::default()) }
    }

    /// `budget_bps` applies until the first `Ack`, whose budget is authoritative.
    pub fn with_thresholds(device_id: u32, session_nonce: u32, theta_pos: f32, theta_vel: f32, budget_bps: u32) -> WasmEdge {
        let cfg = EdgeConfig { theta_pos, theta_vel, budget_bps, ..EdgeConfig::default() };
        WasmEdge { inner: Edge::new(device_id, session_nonce, cfg) }
    }

    /// `tracks_json`: `[{"id":1,"class":0,"pos":[x,y,z],"vel":[x,y,z],"conf":200}, ...]`
    pub fn tick(&mut self, tracks_json: &str, now: u32) -> Vec<u8> {
        let tracks: Vec<Track> = parse_tracks(tracks_json);
        pack(self.inner.tick(&tracks, now))
    }

    pub fn on_datagram(&mut self, bytes: &[u8]) { self.inner.on_datagram(bytes) }
    pub fn set_budget(&mut self, bps: u32) { self.inner.set_budget(bps) }

    /// A `Pose` datagram (metres, marker frame; unit quaternion, w last), or empty before the first
    /// `Ack` and when called sooner than the budget's pose interval: call it at any rate.
    #[allow(clippy::too_many_arguments)]
    pub fn pose(&mut self, px: f32, py: f32, pz: f32, qx: f32, qy: f32, qz: f32, qw: f32, origin_locked: bool, tick: u32) -> Vec<u8> {
        self.inner.pose([px, py, pz], [qx, qy, qz, qw], origin_locked, tick).unwrap_or_default()
    }

    /// `{seq,bytesTotal,deltas,keyframes,updates,thetaScale,acked,keyframeTicks,poseTicks,budgetBps,thetaM}`;
    /// `bytesTotal` is payload only (the link adds 28 B per datagram).
    pub fn stats_json(&self) -> String { self.inner.stats().json() }
}

#[wasm_bindgen]
pub struct WasmReceiver { inner: Receiver }

#[wasm_bindgen]
impl WasmReceiver {
    #[wasm_bindgen(constructor)]
    pub fn new() -> WasmReceiver { WasmReceiver { inner: Receiver::new(ReceiverConfig::default()) } }

    /// Returns a short event description or throws on malformed input.
    pub fn on_datagram(&mut self, bytes: &[u8]) -> Result<String, JsValue> {
        self.inner.on_datagram(bytes).map(|e| format!("{e:?}")).map_err(|e| JsValue::from_str(&format!("{e:?}")))
    }

    pub fn needs_ack(&self) -> bool { self.inner.needs_ack() }
    /// The budget is pushed to the edge and sets this receiver's coast/stale/drop thresholds.
    pub fn make_ack(&mut self, budget_bps: u32) -> Vec<u8> { self.inner.make_ack(budget_bps) }
    pub fn last_edge_tick(&self) -> u32 { self.inner.last_edge_tick() }
    pub fn device_id(&self) -> Option<u32> { self.inner.device_id() }
    pub fn gc(&mut self, at_tick: u32) -> Vec<u32> { self.inner.gc(at_tick) }

    /// JSON array of `{id,class,pos,vel,conf,tick,age,stale,theta,coasting,ce}`: `theta` is the
    /// threshold (m) the edge declared when it last refreshed the entity, `ce` the honest error
    /// radius (m). `coasting` and a growing `ce` are per entity: the device is silent, or it came back
    /// after a blackout and nothing sent since has refreshed that entity.
    pub fn extrapolate_json(&self, at_tick: u32) -> String { extrapolated_json(&self.inner.extrapolate(at_tick)) }

    /// The device has been silent for one keyframe period plus margin (cadence of the advertised budget).
    pub fn coasting(&self, at_tick: u32) -> bool { self.inner.coasting(at_tick) }

    /// Cadence for the budget this receiver last advertised (see `cadence_json`).
    pub fn cadence_json(&self) -> String { self.inner.cadence().json() }

    pub fn pose_json(&self) -> Option<String> {
        self.inner.pose().map(|p| format!(
            "{{\"pos\":[{},{},{}],\"quat\":[{},{},{},{}],\"originLocked\":{},\"tick\":{}}}",
            p.pos[0], p.pos[1], p.pos[2], p.quat[0], p.quat[1], p.quat[2], p.quat[3], p.origin_locked, p.tick
        ))
    }

    pub fn stats_json(&self) -> String {
        let s = self.inner.stats();
        format!(
            "{{\"datagrams\":{},\"bytes\":{},\"deltas\":{},\"keyframes\":{},\"poses\":{},\"gapsDetected\":{},\"nacksSent\":{},\"outOfOrderDropped\":{},\"reconciled\":{}}}",
            s.datagrams, s.bytes, s.deltas, s.keyframes, s.poses, s.gaps_detected, s.nacks_sent, s.out_of_order_dropped, s.reconciled
        )
    }
}

impl Default for WasmReceiver {
    fn default() -> Self { Self::new() }
}

/// Decode a datagram to a debug string (for the viewer's packet log).
#[wasm_bindgen]
pub fn describe(bytes: &[u8]) -> String {
    crate::wire::describe(bytes)
}

/// `{"kind":"hello|delta|keyframe|pose|ack|bye|malformed","deviceId"?,"nonce"?,"seq"?,"tick"?,
/// "ids"?,"part"?,"of"?,"thetaM"?,"error"?}`, for routing and packet logs.
#[wasm_bindgen]
pub fn peek_json(bytes: &[u8]) -> String {
    crate::wire::peek_json(bytes)
}

/// `{"keyframeTicks","helloRefreshTicks","poseTicks","coastTicks","staleTicks","dropTicks"}` for a
/// budget in bit/s (0 = unlimited).
#[wasm_bindgen]
pub fn cadence_json(budget_bps: u32) -> String {
    crate::cadence::cadence(budget_bps).json()
}

/// Split a packed buffer (u16-LE length prefixes) into datagrams. Exposed for tests.
pub fn unpack(buf: &[u8]) -> Vec<Vec<u8>> {
    let mut out = Vec::new();
    let mut i = 0;
    while i + 2 <= buf.len() {
        let n = u16::from_le_bytes([buf[i], buf[i + 1]]) as usize;
        i += 2;
        out.push(buf[i..i + n].to_vec());
        i += n;
    }
    out
}

fn pack(datagrams: Vec<Vec<u8>>) -> Vec<u8> {
    let mut out = Vec::new();
    for d in datagrams {
        out.extend_from_slice(&(d.len() as u16).to_le_bytes());
        out.extend_from_slice(&d);
    }
    out
}

fn parse_tracks(s: &str) -> Vec<Track> {
    #[derive(serde::Deserialize)]
    struct T { id: u32, class: u8, pos: [f32; 3], vel: [f32; 3], conf: u8 }
    serde_json::from_str::<Vec<T>>(s)
        .map(|v| v.into_iter().map(|t| Track { id: t.id, class: t.class, pos: t.pos, vel: t.vel, conf: t.conf }).collect())
        .unwrap_or_default()
}
