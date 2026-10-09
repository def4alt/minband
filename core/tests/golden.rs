//! Determinism contract. A scripted scene runs through Edge and Receiver; the exact datagram
//! bytes and the extrapolated positions are compared with `tests/golden/scene1.json`.
//! The same JSON is replayed under WASM by `server/test/golden.test.ts`.
//! Regenerate with `UPDATE_GOLDEN=1 cargo test --test golden` after an intentional change.

use minband_core::classes::{CHAIR, PERSON};
use minband_core::wire::{encode, Message};
use minband_core::{Edge, EdgeConfig, Receiver, ReceiverConfig, Track, TICK_HZ};
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, PartialEq, Debug)]
struct Golden {
    /// hex of every datagram, in order, with the tick it was produced at
    datagrams: Vec<(u32, String)>,
    /// (tick, id, pos) extrapolated at the receiver every 30 ticks
    extrapolations: Vec<(u32, u32, [f32; 3])>,
    bytes_total: u64,
}

/// Deterministic scene: a walker on a square path with a speed change, a static chair, and a
/// second walker that appears at 3 s and vanishes at 6 s.
pub fn scene(tick: u32) -> Vec<Track> {
    let t = tick as f32 / TICK_HZ as f32;
    let mut v = Vec::new();
    let (x, z, vx, vz) = if t < 2.0 { (t, 0.0, 1.0, 0.0) } else if t < 4.0 { (2.0, (t - 2.0) * 1.5, 0.0, 1.5) } else { (2.0 - (t - 4.0), 3.0, -1.0, 0.0) };
    v.push(Track { id: 1, class: PERSON, pos: [x, 0.0, z], vel: [vx, 0.0, vz], conf: 230 });
    v.push(Track { id: 2, class: CHAIR, pos: [-1.0, 0.0, 1.0], vel: [0.0, 0.0, 0.0], conf: 180 });
    if (3.0..6.0).contains(&t) {
        v.push(Track { id: 3, class: PERSON, pos: [0.0, 0.0, -t], vel: [0.0, 0.0, -1.0], conf: 120 + ((t * 10.0) as u8 % 100) });
    }
    v
}

fn run() -> Golden {
    let mut edge = Edge::new(7, 0xC0FFEE, EdgeConfig::default());
    let mut rx = Receiver::new(ReceiverConfig::default());
    let mut g = Golden { datagrams: Vec::new(), extrapolations: Vec::new(), bytes_total: 0 };
    // Pre-ack so the scene starts immediately (Hello/ack handshake is covered by unit tests).
    edge.on_datagram(&encode(&Message::Ack { last_seq: 0, missing: vec![], budget_bps: 0 }));
    for tick in 0..(8 * TICK_HZ) {
        for d in edge.tick(&scene(tick), tick) {
            // Drop seq 5 to exercise gap -> nack -> repair deterministically.
            let dropped = matches!(minband_core::wire::decode(&d), Ok(Message::Delta { seq: 5, .. }));
            g.datagrams.push((tick, hex(&d)));
            g.bytes_total += d.len() as u64;
            if !dropped {
                rx.on_datagram(&d).unwrap();
            }
        }
        if tick % 12 == 0 && rx.needs_ack() {
            let ack = rx.make_ack(0);
            edge.on_datagram(&ack);
        }
        if tick % 30 == 0 {
            for e in rx.extrapolate(tick) {
                g.extrapolations.push((tick, e.state.id, e.state.pos));
            }
        }
    }
    g
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

#[test]
fn golden_scene1() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/golden/scene1.json");
    let got = run();
    if std::env::var("UPDATE_GOLDEN").is_ok() {
        std::fs::write(path, serde_json::to_string_pretty(&got).unwrap()).unwrap();
        return;
    }
    let want: Golden = serde_json::from_str(&std::fs::read_to_string(path).expect("golden file; run with UPDATE_GOLDEN=1")).unwrap();
    assert_eq!(got.datagrams.len(), want.datagrams.len(), "datagram count");
    for (i, (a, b)) in got.datagrams.iter().zip(&want.datagrams).enumerate() {
        assert_eq!(a, b, "datagram {i} differs");
    }
    assert_eq!(got.extrapolations, want.extrapolations);
    assert_eq!(got.bytes_total, want.bytes_total);
}

#[test]
fn bandwidth_is_proportional_to_surprise() {
    let g = run();
    // 8 s scene, about 3 entities, one keyframe every 2 s. Naive 30 Hz metadata would be
    // 8 * 30 * ~3 * 31 bytes = ~22 kB. We expect well under a tenth of that.
    assert!(g.bytes_total < 2200, "bytes_total = {}", g.bytes_total);
}
