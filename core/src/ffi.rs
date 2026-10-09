//! uniffi surface for Swift (iOS app, `ios/MinBand/EdgeBridge.swift`). Mirrors `wasm.rs`, but with
//! typed records instead of JSON: uniffi carries structs and `Vec<Vec<u8>>` (Swift `[Data]`)
//! natively.
//!
//! uniffi has no fixed-size arrays, so vectors cross as `Vec<f32>`. Short vectors are padded with
//! zeros and extra components ignored; a quaternion that is not exactly 4 long becomes identity.
//! Nothing here panics on bad input, because a panic would abort the Swift caller.
//!
//! uniffi objects are shared (`Arc`) references callable from any thread (the app calls `tick`
//! from the ARKit delegate queue and `on_datagram` from the UDP receive queue), hence the `Mutex`.

use std::sync::{Mutex, MutexGuard};

use crate::cadence::Cadence;
use crate::edge::{Edge, EdgeConfig, EdgeStats, Track};

#[derive(Clone, Debug, PartialEq, uniffi::Record)]
pub struct FfiTrack {
    pub id: u32,
    pub class: u8,
    /// Metres, marker frame, Y up. Length 3.
    pub pos: Vec<f32>,
    /// m/s, marker frame. Length 3.
    pub vel: Vec<f32>,
    pub conf: u8,
}

#[derive(Clone, Copy, Debug, PartialEq, uniffi::Record)]
pub struct FfiEdgeStats {
    pub seq: u32,
    /// Payload bytes of every datagram produced so far (Hello, Delta, Keyframe, Pose). The link
    /// carries 28 B (UDP/IPv4) more per datagram.
    pub bytes_total: u64,
    pub deltas: u32,
    pub keyframes: u32,
    pub updates: u32,
    /// Budget controller multiplier on `theta_pos`/`theta_vel` (1.0 = configured thresholds).
    pub theta_scale: f32,
    pub acked: bool,
    /// Keyframe period and minimum pose interval (ticks) derived from `budget_bps`.
    pub keyframe_ticks: u32,
    pub pose_ticks: u32,
    /// Budget in force (bit/s, 0 = unlimited): the last `Ack`'s, else `set_budget`'s.
    pub budget_bps: u32,
    /// Position threshold in use (m), theta_pos x `theta_scale`: the tolerance bubble radius (V4).
    pub theta_m: f32,
}

impl From<EdgeStats> for FfiEdgeStats {
    fn from(s: EdgeStats) -> Self {
        Self {
            seq: s.seq,
            bytes_total: s.bytes_total,
            deltas: s.deltas,
            keyframes: s.keyframes,
            updates: s.updates,
            theta_scale: s.theta_scale,
            acked: s.acked,
            keyframe_ticks: s.keyframe_ticks,
            pose_ticks: s.pose_ticks,
            budget_bps: s.budget_bps,
            theta_m: s.theta_m,
        }
    }
}

/// Heartbeat cadence for a budget, in ticks of 1/120 s (see `core/src/cadence.rs`).
#[derive(Clone, Copy, Debug, PartialEq, uniffi::Record)]
pub struct FfiCadence {
    pub keyframe_ticks: u32,
    pub hello_refresh_ticks: u32,
    pub pose_ticks: u32,
    pub coast_ticks: u32,
    pub stale_ticks: u32,
    pub drop_ticks: u32,
}

impl From<Cadence> for FfiCadence {
    fn from(c: Cadence) -> Self {
        Self {
            keyframe_ticks: c.keyframe_ticks,
            hello_refresh_ticks: c.hello_refresh_ticks,
            pose_ticks: c.pose_ticks,
            coast_ticks: c.coast_ticks,
            stale_ticks: c.stale_ticks,
            drop_ticks: c.drop_ticks,
        }
    }
}

#[derive(uniffi::Object)]
pub struct FfiEdge {
    inner: Mutex<Edge>,
}

#[uniffi::export]
impl FfiEdge {
    /// Default thresholds (DESIGN §4), unlimited budget until the server sends one.
    #[uniffi::constructor]
    pub fn new(device_id: u32, session_nonce: u32) -> Self {
        Self::from_edge(Edge::new(device_id, session_nonce, EdgeConfig::default()))
    }

    #[uniffi::constructor]
    pub fn with_thresholds(device_id: u32, session_nonce: u32, theta_pos: f32, theta_vel: f32, budget_bps: u32) -> Self {
        let cfg = EdgeConfig { theta_pos, theta_vel, budget_bps, ..EdgeConfig::default() };
        Self::from_edge(Edge::new(device_id, session_nonce, cfg))
    }

    /// Feed the current tracks at edge tick `now` (1/120 s); returns datagrams to send now.
    pub fn tick(&self, tracks: Vec<FfiTrack>, now: u32) -> Vec<Vec<u8>> {
        let tracks: Vec<Track> = tracks.iter().map(track_of).collect();
        self.edge().tick(&tracks, now)
    }

    /// Feed a datagram received from the server (acks, budget).
    pub fn on_datagram(&self, bytes: Vec<u8>) {
        self.edge().on_datagram(&bytes)
    }

    /// Bits per second, 0 = unlimited; also sets the keyframe, Hello and pose cadence. The next
    /// `Ack` overrides it (the server's budget is authoritative, 0 included).
    pub fn set_budget(&self, bps: u32) {
        self.edge().set_budget(bps)
    }

    pub fn stats(&self) -> FfiEdgeStats {
        self.edge().stats().into()
    }

    /// Encode a `Pose` (consumes a seq). `pos`: metres, marker frame. `quat`: unit `[x, y, z, w]`
    /// rotating camera-frame vectors into the marker frame. Empty before the first `Ack` and when
    /// called sooner than the budget's pose interval (`stats().pose_ticks`, 0.5 s unlimited, 10 s
    /// below 4 kbit/s), so it can be called at any rate; callers must not send an empty datagram.
    pub fn encode_pose(&self, pos: Vec<f32>, quat: Vec<f32>, origin_locked: bool, tick: u32) -> Vec<u8> {
        let quat = if quat.len() == 4 { [quat[0], quat[1], quat[2], quat[3]] } else { [0.0, 0.0, 0.0, 1.0] };
        self.edge().pose(vec3(&pos), quat, origin_locked, tick).unwrap_or_default()
    }
}

impl FfiEdge {
    fn from_edge(edge: Edge) -> Self {
        Self { inner: Mutex::new(edge) }
    }

    fn edge(&self) -> MutexGuard<'_, Edge> {
        // Edge never panics while holding the lock; if it ever did, keep serving its state rather
        // than turning every later call into a Swift crash.
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// One-line summary of a datagram for debug logs (same output as the wasm `describe`).
#[uniffi::export]
pub fn describe(bytes: Vec<u8>) -> String {
    crate::wire::describe(&bytes)
}

/// The cadence the edge follows at `budget_bps` (0 = unlimited), e.g. to show the keyframe period.
#[uniffi::export]
pub fn cadence(budget_bps: u32) -> FfiCadence {
    crate::cadence::cadence(budget_bps).into()
}

fn vec3(v: &[f32]) -> [f32; 3] {
    let at = |i: usize| v.get(i).copied().unwrap_or(0.0);
    [at(0), at(1), at(2)]
}

fn track_of(t: &FfiTrack) -> Track {
    Track { id: t.id, class: t.class, pos: vec3(&t.pos), vel: vec3(&t.vel), conf: t.conf }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::classes::PERSON;
    use crate::receiver::{Receiver, ReceiverConfig};
    use crate::wire::{decode, encode, Message, Update};

    fn walker(x: f32) -> FfiTrack {
        FfiTrack { id: 1, class: PERSON, pos: vec![x, 0.0, 0.5], vel: vec![1.0, 0.0, 0.0], conf: 240 }
    }

    fn ack(last_seq: u32, missing: Vec<u32>) -> Vec<u8> {
        encode(&Message::Ack { last_seq, missing, budget_bps: 0 })
    }

    fn ack_budget(budget_bps: u32) -> Vec<u8> {
        encode(&Message::Ack { last_seq: 0, missing: vec![], budget_bps })
    }

    #[test]
    fn tick_roundtrips_through_receiver() {
        let edge = FfiEdge::new(7, 0xC0FFEE);
        let mut rx = Receiver::new(ReceiverConfig::default());

        // Before the first ack: Hello only, and no pose.
        let out = edge.tick(vec![walker(0.0)], 0);
        assert_eq!(out.len(), 1);
        assert!(matches!(decode(&out[0]).unwrap(), Message::Hello { device_id: 7, session_nonce: 0xC0FFEE, .. }));
        assert!(edge.encode_pose(vec![0.0; 3], vec![0.0, 0.0, 0.0, 1.0], true, 0).is_empty());
        rx.on_datagram(&out[0]).unwrap();
        assert!(!edge.stats().acked);

        edge.on_datagram(rx.make_ack(0));
        assert!(edge.stats().acked);

        // Spawn crosses the FFI types intact and lands in the receiver.
        let out = edge.tick(vec![walker(0.25)], 1);
        assert_eq!(out.len(), 1);
        match decode(&out[0]).unwrap() {
            Message::Delta { seq: 1, tick: 1, theta_q: 15, updates } => match updates.as_slice() {
                [Update::Spawn(s)] => {
                    assert_eq!((s.id, s.class, s.pos, s.vel, s.conf), (1, PERSON, [0.25, 0.0, 0.5], [1.0, 0.0, 0.0], 240));
                }
                u => panic!("{u:?}"),
            },
            m => panic!("{m:?}"),
        }
        for d in &out {
            rx.on_datagram(d).unwrap();
        }
        let twin = rx.extrapolate(1);
        assert_eq!(twin.len(), 1);
        assert_eq!(twin[0].state.pos, [0.25, 0.0, 0.5]);

        // Pose consumes the next seq and reaches the receiver.
        let pose = edge.encode_pose(vec![1.0, 1.5, -2.0], vec![0.0, 0.0, 0.0, 1.0], true, 2);
        rx.on_datagram(&pose).unwrap();
        let p = rx.pose().expect("pose");
        assert_eq!((p.pos, p.quat, p.origin_locked, p.tick), ([1.0, 1.5, -2.0], [0.0, 0.0, 0.0, 1.0], true, 2));
        assert_eq!(edge.stats().seq, 2);

        // Track lost: despawn.
        let out = edge.tick(vec![], 3);
        for d in &out {
            rx.on_datagram(d).unwrap();
        }
        assert!(rx.extrapolate(3).is_empty());

        let s = edge.stats();
        assert_eq!((s.seq, s.deltas, s.keyframes, s.updates), (3, 2, 0, 2));
        assert_eq!(s.theta_scale, 1.0);
        assert_eq!(rx.stats().bytes, s.bytes_total, "every produced byte was delivered");
        assert_eq!(describe(out[0].clone()), "Delta seq=3 tick=3 updates=1");
    }

    #[test]
    fn matches_core_edge_bytes() {
        // The FFI layer must not change what goes on the wire.
        let ffi = FfiEdge::with_thresholds(3, 9, 0.1, 0.2, 4000);
        let mut core = Edge::new(3, 9, EdgeConfig { theta_pos: 0.1, theta_vel: 0.2, budget_bps: 4000, ..EdgeConfig::default() });
        ffi.on_datagram(ack_budget(4000));
        core.on_datagram(&ack_budget(4000));
        for tick in 0..600u32 {
            let x = tick as f32 / 100.0;
            let t = walker(x * x);
            let want = core.tick(&[track_of(&t)], tick);
            assert_eq!(ffi.tick(vec![t], tick), want, "tick {tick}");
        }
        assert_eq!(ffi.stats(), FfiEdgeStats::from(core.stats()));
        assert_eq!((ffi.stats().budget_bps, ffi.stats().keyframe_ticks), (4000, cadence(4000).keyframe_ticks));
    }

    #[test]
    fn cadence_and_pose_gating_cross_the_ffi() {
        let edge = FfiEdge::new(1, 1);
        edge.tick(vec![], 0);
        edge.on_datagram(ack_budget(600));
        let c = cadence(600);
        assert_eq!(c, FfiCadence::from(crate::cadence::cadence(600)));
        let s = edge.stats();
        assert_eq!((s.keyframe_ticks, s.pose_ticks, s.budget_bps, s.theta_m), (c.keyframe_ticks, c.pose_ticks, 600, 0.15));
        let q = vec![0.0, 0.0, 0.0, 1.0];
        assert!(!edge.encode_pose(vec![0.0; 3], q.clone(), true, 10).is_empty());
        assert!(edge.encode_pose(vec![0.0; 3], q.clone(), true, 10 + 60).is_empty(), "not due: empty, do not send");
        assert!(!edge.encode_pose(vec![0.0; 3], q, true, 10 + c.pose_ticks).is_empty());
    }

    #[test]
    fn bad_vector_lengths_do_not_panic() {
        let edge = FfiEdge::new(1, 1);
        edge.tick(vec![], 0); // Hello
        edge.on_datagram(ack(0, vec![]));
        edge.set_budget(1000);
        let out = edge.tick(vec![FfiTrack { id: 2, class: PERSON, pos: vec![1.0], vel: vec![], conf: 9 }], 1);
        match decode(&out[0]).unwrap() {
            Message::Delta { updates, .. } => assert!(matches!(updates[0], Update::Spawn(s) if s.pos == [1.0, 0.0, 0.0] && s.vel == [0.0; 3])),
            m => panic!("{m:?}"),
        }
        let pose = edge.encode_pose(vec![1.0, 2.0, 3.0, 4.0], vec![1.0], false, 2);
        assert!(matches!(decode(&pose).unwrap(), Message::Pose { pos: [1.0, 2.0, 3.0], quat: [0.0, 0.0, 0.0, 1.0], .. }));
        edge.on_datagram(vec![0xff, 0x00]); // garbage is ignored
        assert_eq!(describe(vec![]), "error Malformed");
    }
}
