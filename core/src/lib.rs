//! minband-core: the part of MinBand shared by the edge (phone, companion computer) and the
//! receiver (server, browser): the wire codec, the contact manager, the scheduler and the
//! receiver's merge/derive/dead-reckoning. Deterministic where both ends must agree (the receiver's
//! dead reckoning uses f32 basic ops only).
//!
//! - `wire`: frames and records, byte-exact (proto/PROTOCOL.md).
//! - `geo`: rays, the error radius, deterministic sin/cos, ENU -> lat/lon.
//! - `classes`: coarse classes, motion thresholds, speed caps.
//! - `contacts`: tracks -> contacts (motion machine, grouping, revisions).
//! - `scheduler`: timing from the budget; ladder, floor, regimes.
//! - `edge`: the edge object: tick in, frames out; uplink in.
//! - `receiver`: frames in; world, events, digests, focus out.
//! - `wasm` (feature `wasm`): wasm-bindgen surface for Node and the browser.

pub mod classes;
pub mod contacts;
pub mod edge;
pub mod geo;
pub mod receiver;
pub mod scheduler;
pub mod wire;

#[cfg(feature = "wasm")]
pub mod wasm;

/// Ticks per second of the edge clock. 120 divides 30, 60 and 24 fps frame periods exactly.
pub const TICK_HZ: u32 = 120;

pub use contacts::{Contact, ContactConfig, ContactManager, Track};
pub use edge::{Edge, EdgeConfig, EgoInput};
pub use receiver::{Event, Receiver, RxContact};
pub use scheduler::{timing, Regime, Timing};
pub use wire::{Frame, Record};
