//! wasm-bindgen surface for Node and the browser. Tracks come in as JSON (cheap at our rates);
//! datagrams go out as one byte buffer with u16-LE length prefixes to avoid Vec<Vec<u8>>.

use crate::edge::{Edge, EdgeConfig, Track};
use crate::receiver::{Receiver, ReceiverConfig};
use crate::wire::{decode, Message};
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub struct WasmEdge { inner: Edge }

#[wasm_bindgen]
impl WasmEdge {
    #[wasm_bindgen(constructor)]
    pub fn new(device_id: u32, session_nonce: u32) -> WasmEdge {
        WasmEdge { inner: Edge::new(device_id, session_nonce, EdgeConfig::default()) }
    }

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

    pub fn stats_json(&self) -> String {
        let s = self.inner.stats();
        format!(
            "{{\"seq\":{},\"bytesTotal\":{},\"deltas\":{},\"keyframes\":{},\"updates\":{},\"thetaScale\":{},\"acked\":{}}}",
            s.seq, s.bytes_total, s.deltas, s.keyframes, s.updates, s.theta_scale, s.acked
        )
    }
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
    pub fn make_ack(&mut self, budget_bps: u32) -> Vec<u8> { self.inner.make_ack(budget_bps) }
    pub fn last_edge_tick(&self) -> u32 { self.inner.last_edge_tick() }
    pub fn device_id(&self) -> Option<u32> { self.inner.device_id() }
    pub fn gc(&mut self, at_tick: u32) -> Vec<u32> { self.inner.gc(at_tick) }

    /// JSON array of `{id,class,pos,vel,conf,tick,age,stale}`.
    pub fn extrapolate_json(&self, at_tick: u32) -> String {
        let mut s = String::from("[");
        for (i, e) in self.inner.extrapolate(at_tick).iter().enumerate() {
            if i > 0 { s.push(','); }
            let st = e.state;
            s.push_str(&format!(
                "{{\"id\":{},\"class\":{},\"pos\":[{},{},{}],\"vel\":[{},{},{}],\"conf\":{},\"tick\":{},\"age\":{},\"stale\":{}}}",
                st.id, st.class, st.pos[0], st.pos[1], st.pos[2], st.vel[0], st.vel[1], st.vel[2], st.conf, st.tick, e.age_ticks, e.stale
            ));
        }
        s.push(']');
        s
    }

    pub fn pose_json(&self) -> Option<String> {
        self.inner.pose().map(|p| format!(
            "{{\"pos\":[{},{},{}],\"quat\":[{},{},{},{}],\"originLocked\":{},\"tick\":{}}}",
            p.pos[0], p.pos[1], p.pos[2], p.quat[0], p.quat[1], p.quat[2], p.quat[3], p.origin_locked, p.tick
        ))
    }

    pub fn stats_json(&self) -> String {
        let s = self.inner.stats();
        format!(
            "{{\"datagrams\":{},\"bytes\":{},\"deltas\":{},\"keyframes\":{},\"poses\":{},\"gapsDetected\":{},\"nacksSent\":{},\"outOfOrderDropped\":{}}}",
            s.datagrams, s.bytes, s.deltas, s.keyframes, s.poses, s.gaps_detected, s.nacks_sent, s.out_of_order_dropped
        )
    }
}

/// Decode a datagram to a debug string (for the viewer's packet log).
#[wasm_bindgen]
pub fn describe(bytes: &[u8]) -> String {
    match decode(bytes) {
        Ok(Message::Delta { seq, tick, updates }) => format!("Delta seq={seq} tick={tick} updates={}", updates.len()),
        Ok(Message::Keyframe { seq, tick, part, of, entities }) => format!("Keyframe seq={seq} tick={tick} part={part}/{of} entities={}", entities.len()),
        Ok(m) => format!("{m:?}"),
        Err(e) => format!("error {e:?}"),
    }
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
