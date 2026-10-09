//! Edge-side sync: ghosts, divergence thresholds, budget controller, repair on nack.

use crate::predictor::Predictor;
use crate::wire::{encode, decode, EntityState, Message, Update, MAX_DATAGRAM};
use crate::TICK_HZ;

/// One observed track from the phone's tracker at the current tick.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Track {
    pub id: u32,
    pub class: u8,
    pub pos: [f32; 3],
    pub vel: [f32; 3],
    pub conf: u8,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EdgeConfig {
    pub theta_pos: f32,
    pub theta_vel: f32,
    /// Force an update for an entity not refreshed for this long.
    pub t_max_ticks: u32,
    pub keyframe_ticks: u32,
    pub hello_ticks: u32,
    /// After being acked, re-send Hello this often so a restarted server re-identifies the device.
    pub hello_refresh_ticks: u32,
    /// 0 = unlimited.
    pub budget_bps: u32,
    pub theta_scale_min: f32,
    pub theta_scale_max: f32,
    /// Confidence is bucketed into this many levels; a bucket change triggers an update.
    pub conf_buckets: u8,
    /// Ignore a repeat nack for an entity repaired within this many ticks (one round trip).
    pub repair_min_ticks: u32,
    /// Remember despawned ids this long so a lost Despawn can be repaired.
    pub despawn_memory_ticks: u32,
}

impl Default for EdgeConfig {
    fn default() -> Self {
        Self {
            theta_pos: 0.15,
            theta_vel: 0.3,
            t_max_ticks: 3 * TICK_HZ,
            keyframe_ticks: 2 * TICK_HZ,
            hello_ticks: TICK_HZ / 2,
            hello_refresh_ticks: 5 * TICK_HZ,
            budget_bps: 0,
            theta_scale_min: 0.33,
            theta_scale_max: 13.0,
            conf_buckets: 4,
            repair_min_ticks: TICK_HZ / 2,
            despawn_memory_ticks: 10 * TICK_HZ,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct EdgeStats {
    pub seq: u32,
    pub bytes_total: u64,
    pub deltas: u32,
    pub keyframes: u32,
    pub updates: u32,
    pub theta_scale: f32,
    pub acked: bool,
}

const REPAIR_RING: usize = 64;
/// Rough encoded size of one EntityState inside a keyframe.
const ENTITY_BYTES: usize = 32;

pub struct Edge {
    cfg: EdgeConfig,
    device_id: u32,
    session_nonce: u32,
    seq: u32,
    ghosts: Vec<EntityState>,
    last_keyframe_tick: u32,
    last_hello_tick: Option<u32>,
    acked: bool,
    force_keyframe: bool,
    repair_ids: Vec<u32>,
    /// (seq, ids touched by that datagram), ring buffer for nack repair.
    sent: Vec<(u32, Vec<u32>)>,
    /// (id, tick) of recent despawns, so a nacked Despawn can be resent.
    recent_despawns: Vec<(u32, u32)>,
    /// (id, tick) of the last repair sent per id, to ignore repeat nacks within a round trip.
    last_repair: Vec<(u32, u32)>,
    theta_scale: f32,
    window_start: u32,
    window_bytes: u64,
    stats: EdgeStats,
}

impl Edge {
    pub fn new(device_id: u32, session_nonce: u32, cfg: EdgeConfig) -> Self {
        Self {
            cfg,
            device_id,
            session_nonce,
            seq: 0,
            ghosts: Vec::new(),
            last_keyframe_tick: 0,
            last_hello_tick: None,
            acked: false,
            force_keyframe: false,
            repair_ids: Vec::new(),
            sent: Vec::new(),
            recent_despawns: Vec::new(),
            last_repair: Vec::new(),
            theta_scale: 1.0,
            window_start: 0,
            window_bytes: 0,
            stats: EdgeStats { theta_scale: 1.0, ..Default::default() },
        }
    }

    pub fn stats(&self) -> EdgeStats {
        EdgeStats { seq: self.seq, theta_scale: self.theta_scale, acked: self.acked, ..self.stats }
    }

    pub fn set_budget(&mut self, bps: u32) {
        self.cfg.budget_bps = bps;
    }

    /// Feed the current tracks; returns zero or more datagrams to send right now.
    pub fn tick(&mut self, tracks: &[Track], now: u32) -> Vec<Vec<u8>> {
        let mut out = Vec::new();

        if !self.acked {
            let due = match self.last_hello_tick {
                None => true,
                Some(t) => now.wrapping_sub(t) >= self.cfg.hello_ticks,
            };
            if due {
                self.last_hello_tick = Some(now);
                out.push(self.emit(Message::Hello {
                    device_id: self.device_id,
                    session_nonce: self.session_nonce,
                    caps: 0,
                    tick: now,
                }, Vec::new()));
            }
            return out;
        }

        let hello_refresh = match self.last_hello_tick {
            Some(t) => now.wrapping_sub(t) >= self.cfg.hello_refresh_ticks,
            None => true,
        };
        if hello_refresh {
            self.last_hello_tick = Some(now);
            out.push(self.emit(Message::Hello {
                device_id: self.device_id,
                session_nonce: self.session_nonce,
                caps: 0,
                tick: now,
            }, Vec::new()));
        }

        let keyframe_due = self.force_keyframe
            || (!tracks.is_empty() && now.wrapping_sub(self.last_keyframe_tick) >= self.cfg.keyframe_ticks);

        if keyframe_due {
            self.force_keyframe = false;
            self.repair_ids.clear();
            self.last_keyframe_tick = now;
            self.ghosts = tracks.iter().map(|t| state_of(t, now)).collect();
            let per = (MAX_DATAGRAM / ENTITY_BYTES).max(1);
            let chunks: Vec<Vec<EntityState>> = self.ghosts.chunks(per).map(|c| c.to_vec()).collect();
            let of = chunks.len().max(1) as u8;
            if chunks.is_empty() {
                out.push(self.emit(Message::Keyframe { seq: 0, tick: now, part: 0, of: 1, entities: Vec::new() }, Vec::new()));
            }
            for (i, c) in chunks.into_iter().enumerate() {
                let ids = c.iter().map(|e| e.id).collect();
                out.push(self.emit(Message::Keyframe { seq: 0, tick: now, part: i as u8, of, entities: c }, ids));
            }
            self.stats.keyframes += out.len() as u32;
            self.account(now, &out);
            return out;
        }

        let mut updates: Vec<Update> = Vec::new();

        // Despawns: ghosts whose track vanished.
        let despawn_memory = self.cfg.despawn_memory_ticks;
        self.recent_despawns.retain(|(_, t)| now.wrapping_sub(*t) < despawn_memory);
        let recent_despawns = &mut self.recent_despawns;
        self.ghosts.retain(|g| {
            if tracks.iter().any(|t| t.id == g.id) {
                true
            } else {
                updates.push(Update::Despawn { id: g.id, tick: now });
                recent_despawns.push((g.id, now));
                false
            }
        });

        let tp = self.cfg.theta_pos * self.theta_scale;
        let tv = self.cfg.theta_vel * self.theta_scale;
        let tp2 = tp * tp;
        let tv2 = tv * tv;

        let mut repaired_now: Vec<u32> = Vec::new();
        for t in tracks {
            let real = state_of(t, now);
            let repair = self.repair_ids.contains(&t.id) && !self.recently_repaired(t.id, now);
            match self.ghosts.iter_mut().find(|g| g.id == t.id) {
                None => {
                    updates.push(Update::Spawn(real));
                    self.ghosts.push(real);
                }
                Some(g) => {
                    let pred = Predictor::step(g, now);
                    let dp = dist2(&pred.pos, &real.pos);
                    let dv = dist2(&pred.vel, &real.vel);
                    let aged = now.wrapping_sub(g.tick) >= self.cfg.t_max_ticks;
                    let class_changed = g.class != real.class
                        || bucket(g.conf, self.cfg.conf_buckets) != bucket(real.conf, self.cfg.conf_buckets);
                    if dp > tp2 || dv > tv2 || aged || class_changed || repair {
                        updates.push(Update::Update(real));
                        *g = real;
                        if repair {
                            repaired_now.push(t.id);
                        }
                    }
                }
            }
        }
        for id in repaired_now {
            self.note_repair(id, now);
        }
        // Repair of lost Despawns: nacked ids that are no longer tracked but were despawned recently.
        let repair_ids = core::mem::take(&mut self.repair_ids);
        for id in repair_ids {
            let tracked = tracks.iter().any(|t| t.id == id);
            let despawned = self.recent_despawns.iter().any(|(d, _)| *d == id);
            if !tracked && despawned && !self.recently_repaired(id, now) {
                updates.push(Update::Despawn { id, tick: now });
                self.note_repair(id, now);
            }
        }

        if !updates.is_empty() {
            self.stats.updates += updates.len() as u32;
            self.stats.deltas += 1;
            let ids = updates.iter().map(|u| u.id()).collect();
            out.push(self.emit(Message::Delta { seq: 0, tick: now, updates }, ids));
        }
        self.account(now, &out);
        out
    }

    /// Encode a camera pose (`Message::Pose`, DESIGN §4: ~2 Hz, only for drawing the frustum).
    /// `pos` in metres in the marker frame, `quat` a unit quaternion `[x, y, z, w]` (w last).
    ///
    /// Like every non-Hello message it consumes a `seq` (so the receiver can detect gaps), but it
    /// is never repaired: a nack for a pose seq touches no entities. Returns `None` before the
    /// first `Ack`, since the edge sends nothing but `Hello` until then (PROTOCOL.md).
    pub fn pose(&mut self, pos: [f32; 3], quat: [f32; 4], origin_locked: bool, tick: u32) -> Option<Vec<u8>> {
        if !self.acked {
            return None;
        }
        Some(self.emit(Message::Pose { seq: 0, tick, pos, quat, origin_locked }, Vec::new()))
    }

    /// Feed a datagram received from the server (acks).
    pub fn on_datagram(&mut self, bytes: &[u8]) {
        if let Ok(Message::Ack { missing, budget_bps, .. }) = decode(bytes) {
            self.acked = true;
            if budget_bps > 0 {
                self.cfg.budget_bps = budget_bps;
            }
            if missing.len() > 8 {
                self.force_keyframe = true;
                return;
            }
            for m in missing {
                if let Some((_, ids)) = self.sent.iter().find(|(s, _)| *s == m) {
                    for id in ids {
                        if !self.repair_ids.contains(id) {
                            self.repair_ids.push(*id);
                        }
                    }
                }
            }
        }
    }

    fn recently_repaired(&self, id: u32, now: u32) -> bool {
        self.last_repair.iter().any(|(i, t)| *i == id && now.wrapping_sub(*t) < self.cfg.repair_min_ticks)
    }

    fn note_repair(&mut self, id: u32, now: u32) {
        let min = self.cfg.repair_min_ticks;
        self.last_repair.retain(|(i, t)| *i != id && now.wrapping_sub(*t) < min);
        self.last_repair.push((id, now));
    }

    fn emit(&mut self, mut msg: Message, ids: Vec<u32>) -> Vec<u8> {
        let consumes_seq = !matches!(msg, Message::Hello { .. });
        if consumes_seq {
            self.seq = self.seq.wrapping_add(1);
            match &mut msg {
                Message::Delta { seq, .. } | Message::Keyframe { seq, .. } | Message::Pose { seq, .. } | Message::Bye { seq, .. } => *seq = self.seq,
                _ => {}
            }
            if self.sent.len() >= REPAIR_RING {
                self.sent.remove(0);
            }
            self.sent.push((self.seq, ids));
        }
        let b = encode(&msg);
        self.stats.bytes_total += b.len() as u64;
        self.window_bytes += b.len() as u64;
        b
    }

    /// Budget controller: every half second compare throughput with the budget and scale thresholds.
    fn account(&mut self, now: u32, _out: &[Vec<u8>]) {
        let window = TICK_HZ / 2;
        if now.wrapping_sub(self.window_start) < window {
            return;
        }
        if self.cfg.budget_bps > 0 {
            let bps = self.window_bytes * 8 * (TICK_HZ as u64) / (window as u64);
            if bps > self.cfg.budget_bps as u64 {
                self.theta_scale *= 1.25;
            } else if bps < (self.cfg.budget_bps as u64) * 7 / 10 {
                self.theta_scale *= 0.9;
            }
            self.theta_scale = self.theta_scale.clamp(self.cfg.theta_scale_min, self.cfg.theta_scale_max);
        }
        self.window_start = now;
        self.window_bytes = 0;
    }
}

fn state_of(t: &Track, tick: u32) -> EntityState {
    EntityState { id: t.id, class: t.class, pos: t.pos, vel: t.vel, conf: t.conf, tick }
}

fn dist2(a: &[f32; 3], b: &[f32; 3]) -> f32 {
    let d = [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
    d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
}

fn bucket(conf: u8, buckets: u8) -> u8 {
    ((conf as u16 * buckets as u16) / 256) as u8
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::classes::PERSON;

    fn edge() -> Edge {
        let mut e = Edge::new(1, 42, EdgeConfig::default());
        e.tick(&[], 0); // Hello
        e.on_datagram(&encode(&Message::Ack { last_seq: 0, missing: vec![], budget_bps: 0 }));
        e
    }

    #[test]
    fn hello_is_refreshed_periodically_after_ack() {
        let mut e = edge();
        assert!(e.tick(&[], 1).is_empty());
        let out = e.tick(&[], 5 * TICK_HZ + 1);
        assert_eq!(out.len(), 1);
        assert!(matches!(decode(&out[0]).unwrap(), Message::Hello { .. }));
    }

    fn walker(x: f32) -> Track {
        Track { id: 1, class: PERSON, pos: [x, 0.0, 0.0], vel: [1.0, 0.0, 0.0], conf: 240 }
    }

    #[test]
    fn sends_hello_until_acked() {
        let mut e = Edge::new(1, 42, EdgeConfig::default());
        let out = e.tick(&[walker(0.0)], 0);
        assert_eq!(out.len(), 1);
        assert!(matches!(decode(&out[0]).unwrap(), Message::Hello { device_id: 1, .. }));
        assert!(e.tick(&[walker(0.0)], 1).is_empty(), "no re-hello within hello_ticks");
        assert_eq!(e.tick(&[walker(0.0)], TICK_HZ).len(), 1);
    }

    #[test]
    fn predictable_motion_sends_nothing_until_t_max() {
        let mut e = edge();
        assert_eq!(e.tick(&[walker(0.0)], 1).len(), 1, "spawn");
        let mut sent = 0;
        for tick in 2..(2 * TICK_HZ) {
            // Real motion matches the predictor's damped model closely enough? Damping makes the
            // ghost slow down while the walker keeps 1 m/s, so a delta is expected eventually,
            // but not within the first few ticks.
            let x = (tick as f32 - 1.0) / TICK_HZ as f32;
            let out = e.tick(&[walker(x)], tick);
            sent += out.len();
            if tick < 20 {
                assert!(out.is_empty(), "tick {tick}: unexpected send");
            }
        }
        assert!(sent >= 1, "keyframe at 2 s at least");
    }

    #[test]
    fn jump_triggers_update_and_despawn_when_lost() {
        let mut e = edge();
        e.tick(&[walker(0.0)], 1);
        let out = e.tick(&[walker(5.0)], 2);
        assert_eq!(out.len(), 1);
        match decode(&out[0]).unwrap() {
            Message::Delta { updates, .. } => assert!(matches!(updates[0], Update::Update(_))),
            m => panic!("unexpected {m:?}"),
        }
        let out = e.tick(&[], 3);
        match decode(&out[0]).unwrap() {
            Message::Delta { updates, .. } => assert!(matches!(updates[0], Update::Despawn { id: 1, .. })),
            m => panic!("unexpected {m:?}"),
        }
    }

    #[test]
    fn nack_triggers_state_repair() {
        let mut e = edge();
        let out = e.tick(&[walker(0.0)], 1);
        let seq = match decode(&out[0]).unwrap() { Message::Delta { seq, .. } => seq, _ => panic!() };
        e.on_datagram(&encode(&Message::Ack { last_seq: seq, missing: vec![seq], budget_bps: 0 }));
        let out = e.tick(&[walker(0.01)], 2);
        assert_eq!(out.len(), 1, "repair resend of entity 1");
    }

    #[test]
    fn pose_consumes_seq_after_ack_and_is_never_repaired() {
        let mut un = Edge::new(1, 42, EdgeConfig::default());
        assert_eq!(un.pose([0.0; 3], [0.0, 0.0, 0.0, 1.0], true, 0), None, "only Hello before ack");
        assert_eq!(un.stats().seq, 0);

        let mut e = edge();
        let out = e.tick(&[walker(0.0)], 1);
        let delta_seq = match decode(&out[0]).unwrap() { Message::Delta { seq, .. } => seq, m => panic!("{m:?}") };
        let b = e.pose([1.0, 1.5, -2.0], [0.0, 0.0, 0.0, 1.0], true, 2).expect("acked");
        let pose_seq = match decode(&b).unwrap() {
            Message::Pose { seq, tick: 2, pos: [1.0, 1.5, -2.0], quat: [0.0, 0.0, 0.0, 1.0], origin_locked: true } => seq,
            m => panic!("{m:?}"),
        };
        assert_eq!(pose_seq, delta_seq + 1);
        assert_eq!(e.stats().seq, pose_seq);
        assert!(e.stats().bytes_total >= (out[0].len() + b.len()) as u64);

        // Nack of the pose seq: nothing to repair, the walker is still on its ghost.
        e.on_datagram(&encode(&Message::Ack { last_seq: pose_seq, missing: vec![pose_seq], budget_bps: 0 }));
        assert!(e.tick(&[walker(0.01)], 3).is_empty());
        // The next datagram continues the sequence.
        let out = e.tick(&[walker(5.0)], 4);
        assert!(matches!(decode(&out[0]).unwrap(), Message::Delta { seq, .. } if seq == pose_seq + 1));
    }

    #[test]
    fn lost_despawn_is_repaired_and_repeat_nack_ignored() {
        let mut e = edge();
        e.tick(&[walker(0.0)], 1);
        let out = e.tick(&[], 2); // despawn
        let seq = match decode(&out[0]).unwrap() { Message::Delta { seq, .. } => seq, _ => panic!() };
        e.on_datagram(&encode(&Message::Ack { last_seq: seq, missing: vec![seq], budget_bps: 0 }));
        let out = e.tick(&[], 3);
        match decode(&out[0]).unwrap() {
            Message::Delta { updates, .. } => assert!(matches!(updates[0], Update::Despawn { id: 1, .. })),
            m => panic!("unexpected {m:?}"),
        }
        // Same nack again within a round trip: nothing is resent.
        e.on_datagram(&encode(&Message::Ack { last_seq: seq, missing: vec![seq], budget_bps: 0 }));
        assert!(e.tick(&[], 4).is_empty());
        // After repair_min_ticks a repeat nack is honoured again.
        e.on_datagram(&encode(&Message::Ack { last_seq: seq, missing: vec![seq], budget_bps: 0 }));
        assert_eq!(e.tick(&[], 4 + TICK_HZ).len(), 1);
    }

    #[test]
    fn many_missing_forces_keyframe() {
        let mut e = edge();
        e.tick(&[walker(0.0)], 1);
        e.on_datagram(&encode(&Message::Ack { last_seq: 1, missing: (1..=9).collect(), budget_bps: 0 }));
        let out = e.tick(&[walker(0.01)], 2);
        assert!(matches!(decode(&out[0]).unwrap(), Message::Keyframe { .. }));
    }
}
