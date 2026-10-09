//! minband-core: the part of MinBand that must behave identically on the edge (iOS) and the
//! twin (Node/browser). Everything here is deterministic: f32 state, integer ticks, only IEEE
//! basic ops (+ - * / sqrt, comparisons). No trig, no exp, no platform libm.
//!
//! Modules:
//! - `wire`: message types and the postcard codec.
//! - `predictor`: per-class kinematic extrapolation.
//! - `edge`: ghosts, divergence thresholds, budget controller, outgoing sequence.
//! - `receiver`: per-device state, gap detection, acks, extrapolation.




pub mod classes;
pub mod edge;
pub mod predictor;
pub mod receiver;
pub mod wire;

#[cfg(feature = "wasm")]
pub mod wasm;

/// Ticks per second of the edge clock. 120 divides 30, 60 and 24 fps frame periods exactly.
pub const TICK_HZ: u32 = 120;

pub use edge::{Edge, EdgeConfig, Track};
pub use predictor::Predictor;
pub use receiver::{Receiver, ReceiverConfig};
pub use wire::{EntityState, Message, Update};
