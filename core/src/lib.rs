//! minband-core: the part of MinBand that must behave identically on the edge (iOS) and the
//! twin (Node/browser). Everything here is deterministic: f32 state, integer ticks, only IEEE
//! basic ops (+ - * / sqrt, comparisons). No trig, no exp, no platform libm.
//!
//! Modules:
//! - `wire`: message types and the postcard codec.
//! - `predictor`: per-class kinematic extrapolation.
//! - `cadence`: keyframe/hello/pose periods and coast/stale/drop thresholds from the byte budget.
//! - `edge`: ghosts, divergence thresholds, budget controller, outgoing sequence.
//! - `receiver`: per-device state, gap detection, acks, extrapolation.
//! - `wasm` (feature `wasm`): wasm-bindgen surface for the server and viewer.
//! - `ffi` (feature `ios`): uniffi surface for the Swift app (`tools/build-ios.sh`).

pub mod cadence;
pub mod classes;
pub mod edge;
pub mod predictor;
pub mod receiver;
pub mod wire;

#[cfg(feature = "wasm")]
pub mod wasm;

#[cfg(feature = "ios")]
pub mod ffi;

// uniffi needs its scaffolding (`UniFfiTag`, namespace `minband_core`) at the crate root.
#[cfg(feature = "ios")]
uniffi::setup_scaffolding!();

/// Ticks per second of the edge clock. 120 divides 30, 60 and 24 fps frame periods exactly.
pub const TICK_HZ: u32 = 120;

pub use cadence::{cadence, Cadence};
pub use edge::{Edge, EdgeConfig, Track};
pub use predictor::Predictor;
pub use receiver::{Receiver, ReceiverConfig};
pub use wire::{EntityState, Message, Update};
