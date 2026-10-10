//! wasm-bindgen surface for Node and the browser. Structured data crosses as JSON strings (cheap
//! at our rates); frames as byte buffers, several packed with u16-LE length prefixes.

use crate::edge::{Edge, EdgeConfig, EgoInput};
use crate::receiver::Receiver;
use crate::wire::{describe, peek_json};
use crate::contacts::Track;
use wasm_bindgen::prelude::*;

fn pack(frames: Vec<Vec<u8>>) -> Vec<u8> {
    let mut out = Vec::with_capacity(frames.iter().map(|f| f.len() + 2).sum());
    for f in frames { out.extend_from_slice(&(f.len() as u16).to_le_bytes()); out.extend_from_slice(&f); }
    out
}
fn js(e: impl std::fmt::Debug) -> JsValue { JsValue::from_str(&format!("{e:?}")) }

#[wasm_bindgen]
pub struct WasmEdge { inner: Edge }

#[wasm_bindgen]
impl WasmEdge {
    /// `config_json`: any subset of `EdgeConfig` fields (snake_case), the rest default.
    #[wasm_bindgen(constructor)]
    pub fn new(config_json: &str) -> Result<WasmEdge, JsValue> {
        let cfg: EdgeConfig = if config_json.trim().is_empty() { EdgeConfig::default() } else { serde_json::from_str(config_json).map_err(js)? };
        Ok(WasmEdge { inner: Edge::new(cfg) })
    }
    /// `tracks_json`: `[{"id":1,"class":2,"e":..,"n":..,"ve":..,"vn":..,"conf":200,"ce":null,"bbox":[u,v,w,h]|null}]`;
    /// `ego_json`: `EgoInput` fields. Returns packed frames (u16-LE length prefix each).
    pub fn tick(&mut self, tracks_json: &str, ego_json: &str, now: u32) -> Result<Vec<u8>, JsValue> {
        let tracks: Vec<Track> = serde_json::from_str(tracks_json).map_err(js)?;
        let ego: EgoInput = if ego_json.trim().is_empty() { EgoInput::default() } else { serde_json::from_str(ego_json).map_err(js)? };
        Ok(pack(self.inner.tick(&tracks, &ego, now)))
    }
    pub fn pose(&mut self, tick: u32, e: f32, n: f32, up: f32, yaw: f32, pitch: f32, roll: f32) { self.inner.pose(tick, e, n, up, yaw, pitch, roll) }
    pub fn on_uplink(&mut self, bytes: &[u8], now: u32) -> Result<(), JsValue> { self.inner.on_uplink(bytes, now).map_err(js) }
    pub fn set_budget(&mut self, bps: u32) { self.inner.set_budget(bps) }
    pub fn snapshot_json(&self, now: u32) -> String { serde_json::to_string(&self.inner.snapshot(now)).unwrap_or_default() }
    pub fn stats_json(&self) -> String { serde_json::to_string(&self.inner.stats).unwrap_or_default() }
    pub fn timing_json(&self) -> String { serde_json::to_string(self.inner.timing()).unwrap_or_default() }
}

#[wasm_bindgen]
pub struct WasmReceiver { inner: Receiver, seq: u16 }

#[wasm_bindgen]
impl WasmReceiver {
    #[wasm_bindgen(constructor)]
    pub fn new(budget_bps: u32) -> WasmReceiver { WasmReceiver { inner: Receiver::new(budget_bps), seq: 0 } }
    /// Applies a frame; returns the records applied or throws on a malformed frame.
    pub fn on_frame(&mut self, bytes: &[u8]) -> Result<usize, JsValue> { self.inner.on_frame(bytes).map_err(js) }
    /// JSON array of `RxContact` at edge tick `now`.
    pub fn snapshot_json(&self, now: u32) -> String { serde_json::to_string(&self.inner.snapshot(now)).unwrap_or_default() }
    /// Drains the derived events as JSON.
    pub fn events_json(&mut self) -> String { serde_json::to_string(&self.inner.drain_events()).unwrap_or_default() }
    pub fn ego_json(&self) -> String {
        match self.inner.ego { Some((e, t)) => format!("{{\"rec\":{},\"tick\":{}}}", serde_json::to_string(&e).unwrap_or_default(), t), None => "null".into() }
    }
    pub fn session_json(&self) -> String { self.inner.session.map(|s| serde_json::to_string(&s).unwrap_or_default()).unwrap_or_else(|| "null".into()) }
    pub fn poses_json(&self) -> String { serde_json::to_string(&self.inner.poses).unwrap_or_default() }
    pub fn stats_json(&self) -> String { serde_json::to_string(&self.inner.stats).unwrap_or_default() }
    pub fn timing_json(&self) -> String { serde_json::to_string(self.inner.timing()).unwrap_or_default() }
    pub fn known(&self) -> u32 { self.inner.known_of().0 }
    pub fn of(&self) -> i32 { self.inner.known_of().1.map_or(-1, |n| n as i32) }
    pub fn last_tick(&self) -> u32 { self.inner.last_tick() }
    pub fn device_unheard(&self, now: u32) -> bool { self.inner.device_unheard(now) }
    pub fn needs_digest(&self) -> bool { self.inner.needs_digest() }
    pub fn make_digest(&mut self, budget_bps: u32, now: u32) -> Vec<u8> { let s = self.seq; self.seq = s.wrapping_add(1); self.inner.make_digest(budget_bps, s, now) }
    pub fn make_focus(&mut self, id: u16, mode: u8, ttl: u8, chip_px: u8, now: u32) -> Vec<u8> { let s = self.seq; self.seq = s.wrapping_add(1); self.inner.make_focus(id, mode, ttl, chip_px, s, now) }
    pub fn gc(&mut self, now: u32) { self.inner.gc(now) }
    pub fn set_budget(&mut self, bps: u32) { self.inner.set_budget(bps) }
}

#[wasm_bindgen]
pub fn peek(bytes: &[u8]) -> String { peek_json(bytes) }

/// One line per record, joined with newlines.
#[wasm_bindgen]
pub fn describe_frame(bytes: &[u8]) -> String { describe(bytes).join("\n") }
