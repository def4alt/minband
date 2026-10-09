//! Edge-side sync: ghosts, divergence thresholds, budget controller, repair on nack.

use crate::cadence::cadence;
use crate::predictor::Predictor;
use crate::wire::{decode, encode, theta_q, EntityState, Message, Update, MAX_DATAGRAM, UDP_IP_OVERHEAD};
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

/// `keyframe_ticks`, `hello_refresh_ticks`, `pose_ticks` and `t_max_ticks` are the values used at
/// budget 0; any other budget replaces them with [`cadence`]`(budget_bps)`, the age cap with 1.5
/// keyframe periods as at budget 0 (budget 0 restores them).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EdgeConfig {
    pub theta_pos: f32,
    pub theta_vel: f32,
    /// Force an update for an entity not refreshed for this long. Longer than the keyframe period,
    /// so it never fires while keyframes arrive (else it would undo a slow keyframe cadence).
    pub t_max_ticks: u32,
    pub keyframe_ticks: u32,
    pub hello_ticks: u32,
    /// After being acked, re-send Hello this often so a restarted server re-identifies the device.
    pub hello_refresh_ticks: u32,
    /// `pose()` returns None when called sooner than this after the last pose it encoded.
    pub pose_ticks: u32,
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
            pose_ticks: TICK_HZ / 2,
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
    /// Payload bytes of every datagram produced so far (Hello, Delta, Keyframe, Pose). The link
    /// carries `UDP_IP_OVERHEAD` more per datagram; the budget controller counts those too.
    pub bytes_total: u64,
    pub deltas: u32,
    /// Keyframe datagrams (parts).
    pub keyframes: u32,
    pub updates: u32,
    pub theta_scale: f32,
    pub acked: bool,
    /// Current cadence (from the budget) and budget.
    pub keyframe_ticks: u32,
    pub pose_ticks: u32,
    pub budget_bps: u32,
    /// Position threshold in use, θ_pos x theta_scale (m); the wire carries it rounded up.
    pub theta_m: f32,
}

impl EdgeStats {
    pub fn json(&self) -> String {
        format!(
            "{{\"seq\":{},\"bytesTotal\":{},\"deltas\":{},\"keyframes\":{},\"updates\":{},\"thetaScale\":{},\"acked\":{},\"keyframeTicks\":{},\"poseTicks\":{},\"budgetBps\":{},\"thetaM\":{}}}",
            self.seq, self.bytes_total, self.deltas, self.keyframes, self.updates, self.theta_scale, self.acked,
            self.keyframe_ticks, self.pose_ticks, self.budget_bps, self.theta_m
        )
    }
}

const REPAIR_RING: usize = 64;
/// Upper estimate of one EntityState inside a keyframe (30 B typical, up to 36 with large ids and
/// ticks; `wire::tests::typical_sizes`).
const ENTITY_BYTES: usize = 32;
/// Keyframe bytes besides its entities: version, kind, seq, tick, theta_q, part, of, length.
const KF_HEADER_BYTES: usize = 12;
/// Paced keyframe parts are at least one ack interval apart.
const KF_PART_MIN_TICKS: u32 = TICK_HZ / 10;
/// Seconds of link time one paced part may take: bounds how long it holds the radio (and the deltas
/// queued behind it) without paying a 39 B header per entity at the lowest rates (3 entities per
/// part at 600 bit/s, 10 at 1500, a full datagram from ~5 kbit/s).
const KF_PART_LINK_S: usize = 2;
/// Budget controller window: long enough for this many one-update Deltas (68 B on the wire) at the
/// budget (7.3 s at 450 bit/s, 3.3 s at 1000), between half a second and ten seconds.
const CTRL_WINDOW_DATAGRAMS: u64 = 6;
const CTRL_DATAGRAM_BITS: u64 = 68 * 8;
const CTRL_WINDOW_MAX: u32 = 10 * TICK_HZ;
/// `pose()` slack: callers time poses on their own clock (iOS: frame timestamps), so a call one
/// frame early still counts as due.
const POSE_SLACK_TICKS: u32 = TICK_HZ / 30;

/// A keyframe being sent one part at a time (S16). The entity list is fixed when it starts (its
/// `tick`); each part carries the *current* state of its entities when it goes out, so ghosts are
/// reset only for what was actually sent and the delta path keeps running for the rest.
struct PacedKeyframe {
    tick: u32,
    parts: Vec<Vec<u32>>,
    next: usize,
    due: u32,
}

pub struct Edge {
    cfg: EdgeConfig,
    /// (keyframe, hello refresh, pose, age cap) ticks from the constructor config: budget 0.
    base: (u32, u32, u32, u32),
    device_id: u32,
    session_nonce: u32,
    seq: u32,
    ghosts: Vec<EntityState>,
    last_keyframe_tick: u32,
    last_hello_tick: Option<u32>,
    last_pose_tick: Option<u32>,
    acked: bool,
    force_keyframe: bool,
    paced: Option<PacedKeyframe>,
    repair_ids: Vec<u32>,
    /// (seq, ids touched by that datagram), ring buffer for nack repair.
    sent: Vec<(u32, Vec<u32>)>,
    /// (id, tick) of recent despawns, so a nacked Despawn can be resent.
    recent_despawns: Vec<(u32, u32)>,
    /// (id, tick) of the last repair sent per id, to ignore repeat nacks within a round trip.
    last_repair: Vec<(u32, u32)>,
    theta_scale: f32,
    /// Start of the budget controller's current window.
    window_start: u32,
    /// Bits on the link (payload + UDP/IP header) since `window_start`.
    window_bits: u64,
    stats: EdgeStats,
}

impl Edge {
    pub fn new(device_id: u32, session_nonce: u32, cfg: EdgeConfig) -> Self {
        let mut e = Self {
            cfg,
            base: (cfg.keyframe_ticks, cfg.hello_refresh_ticks, cfg.pose_ticks, cfg.t_max_ticks),
            device_id,
            session_nonce,
            seq: 0,
            ghosts: Vec::new(),
            last_keyframe_tick: 0,
            last_hello_tick: None,
            last_pose_tick: None,
            acked: false,
            force_keyframe: false,
            paced: None,
            repair_ids: Vec::new(),
            sent: Vec::new(),
            recent_despawns: Vec::new(),
            last_repair: Vec::new(),
            theta_scale: 1.0,
            window_start: 0,
            window_bits: 0,
            stats: EdgeStats { theta_scale: 1.0, ..Default::default() },
        };
        e.apply_cadence();
        e
    }

    pub fn stats(&self) -> EdgeStats {
        EdgeStats {
            seq: self.seq,
            theta_scale: self.theta_scale,
            acked: self.acked,
            keyframe_ticks: self.cfg.keyframe_ticks,
            pose_ticks: self.cfg.pose_ticks,
            budget_bps: self.cfg.budget_bps,
            theta_m: self.cfg.theta_pos * self.theta_scale,
            ..self.stats
        }
    }

    /// The receiver's model of every entity (what it believes, as of each entity's last send).
    pub fn ghosts(&self) -> &[EntityState] { &self.ghosts }

    /// Bits per second, 0 = unlimited. Also sets the keyframe, Hello and pose cadence (S19). The
    /// next `Ack` overrides it: the server's budget is authoritative once it acks.
    pub fn set_budget(&mut self, bps: u32) {
        self.cfg.budget_bps = bps;
        self.window_bits = 0;
        self.apply_cadence();
    }

    fn apply_cadence(&mut self) {
        let (kf, hello, pose, t_max) = if self.cfg.budget_bps == 0 {
            self.base
        } else {
            let c = cadence(self.cfg.budget_bps);
            (c.keyframe_ticks, c.hello_refresh_ticks, c.pose_ticks, c.keyframe_ticks + c.keyframe_ticks / 2)
        };
        self.cfg.keyframe_ticks = kf;
        self.cfg.hello_refresh_ticks = hello;
        self.cfg.pose_ticks = pose;
        self.cfg.t_max_ticks = t_max;
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

        // The keyframe is also the heartbeat: it goes out on schedule even when nothing is
        // tracked, so the receiver can tell an empty scene from a dead link (coasting, S15) and a
        // lost Despawn of the last entity is reconciled. A periodic keyframe waits for a paced
        // one still in flight; a forced one (nack) replaces it.
        let keyframe_due = self.force_keyframe
            || (self.paced.is_none() && now.wrapping_sub(self.last_keyframe_tick) >= self.cfg.keyframe_ticks);

        if keyframe_due {
            self.force_keyframe = false;
            self.last_keyframe_tick = now;
            if self.cfg.budget_bps == 0 {
                // Unlimited: the whole keyframe now, as one snapshot.
                self.paced = None;
                self.repair_ids.clear();
                self.ghosts = tracks.iter().map(|t| state_of(t, now)).collect();
                let per = (MAX_DATAGRAM / ENTITY_BYTES).max(1);
                let chunks: Vec<Vec<EntityState>> = self.ghosts.chunks(per).map(|c| c.to_vec()).collect();
                let of = chunks.len().max(1) as u8;
                if chunks.is_empty() {
                    out.push(self.emit(Message::Keyframe { seq: 0, tick: now, theta_q: 0, part: 0, of: 1, entities: Vec::new() }, Vec::new()));
                }
                for (i, c) in chunks.into_iter().enumerate() {
                    let ids = c.iter().map(|e| e.id).collect();
                    out.push(self.emit(Message::Keyframe { seq: 0, tick: now, theta_q: 0, part: i as u8, of, entities: c }, ids));
                }
                self.stats.keyframes += of as u32;
                self.account(now);
                return out;
            }
            let ids: Vec<u32> = tracks.iter().map(|t| t.id).collect();
            let per = part_entities(self.cfg.budget_bps, ids.len());
            let parts = if ids.is_empty() { vec![Vec::new()] } else { ids.chunks(per).map(|c| c.to_vec()).collect() };
            self.paced = Some(PacedKeyframe { tick: now, parts, next: 0, due: now });
        }
        self.send_due_parts(tracks, now, &mut out);

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
            out.push(self.emit(Message::Delta { seq: 0, tick: now, theta_q: 0, updates }, ids));
        }
        self.account(now);
        out
    }

    /// Paced keyframe parts due at `now`: the current state of each listed entity still tracked,
    /// then the next part after the link time of this one at the budget (all at once at budget 0).
    fn send_due_parts(&mut self, tracks: &[Track], now: u32, out: &mut Vec<Vec<u8>>) {
        while let Some(p) = &self.paced {
            if self.cfg.budget_bps != 0 && now.wrapping_sub(p.due) >= u32::MAX / 2 {
                return; // not due yet
            }
            let (tick, part, of) = (p.tick, p.next, p.parts.len() as u8);
            let entities: Vec<EntityState> = p.parts[part]
                .iter()
                .filter_map(|id| tracks.iter().find(|t| t.id == *id))
                .map(|t| state_of(t, now))
                .collect();
            let ids: Vec<u32> = entities.iter().map(|e| e.id).collect();
            for e in &entities {
                match self.ghosts.iter_mut().find(|g| g.id == e.id) {
                    Some(g) => *g = *e,
                    None => self.ghosts.push(*e),
                }
            }
            self.repair_ids.retain(|id| !ids.contains(id));
            let b = self.emit(Message::Keyframe { seq: 0, tick, theta_q: 0, part: part as u8, of, entities }, ids);
            let gap = pace_ticks(b.len() + UDP_IP_OVERHEAD, self.cfg.budget_bps);
            out.push(b);
            self.stats.keyframes += 1;
            let p = self.paced.as_mut().expect("checked above");
            p.next += 1;
            p.due = now.wrapping_add(gap);
            if p.next >= p.parts.len() {
                self.paced = None;
            }
        }
    }

    /// Encode a camera pose (`Message::Pose`, only for drawing the frustum). `pos` in metres in the
    /// marker frame, `quat` a unit quaternion `[x, y, z, w]` (w last).
    ///
    /// Like every non-Hello message it consumes a `seq` (so the receiver can detect gaps), but it
    /// is never repaired: a nack for a pose seq touches no entities. Returns `None` before the
    /// first `Ack` (the edge sends nothing but `Hello` until then, PROTOCOL.md) and when called
    /// sooner than the budget's `pose_ticks` after the last pose, so callers just call it at their
    /// own rate.
    pub fn pose(&mut self, pos: [f32; 3], quat: [f32; 4], origin_locked: bool, tick: u32) -> Option<Vec<u8>> {
        if !self.acked {
            return None;
        }
        if let Some(t) = self.last_pose_tick {
            let d = tick.wrapping_sub(t);
            if d < u32::MAX / 2 && d + POSE_SLACK_TICKS < self.cfg.pose_ticks {
                return None;
            }
        }
        self.last_pose_tick = Some(tick);
        Some(self.emit(Message::Pose { seq: 0, tick, pos, quat, origin_locked }, Vec::new()))
    }

    /// Feed a datagram received from the server (acks). The ack's `budget_bps` is authoritative
    /// (0 = unlimited), so the edge always runs the cadence the receiver derives from it.
    pub fn on_datagram(&mut self, bytes: &[u8]) {
        if let Ok(Message::Ack { missing, budget_bps, .. }) = decode(bytes) {
            self.acked = true;
            if budget_bps != self.cfg.budget_bps {
                self.set_budget(budget_bps);
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
        let theta = theta_q(self.cfg.theta_pos * self.theta_scale);
        if let Message::Delta { theta_q, .. } | Message::Keyframe { theta_q, .. } = &mut msg {
            *theta_q = theta;
        }
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
        // The controller targets what the link carries: payload plus the UDP/IP header.
        self.window_bits += ((b.len() + UDP_IP_OVERHEAD) * 8) as u64;
        b
    }

    /// Budget controller: compare the bits on the link over a window with the budget; widen the
    /// thresholds (x1.25) when over, narrow them (x0.9) under 80 %. The window is half a second or,
    /// at low budgets, long enough to hold `CTRL_WINDOW_DATAGRAMS` one-update datagrams, so one
    /// datagram reads as a sixth of the budget: with a fixed 0.5 s window one 68 B datagram read
    /// 1088 bit/s, so below ~2 kbit/s every packet widened and only empty windows narrowed, which
    /// settles at ~0.64 datagrams/s whatever the budget (35 % of 1000 bit/s). A window whose whole
    /// allowance is spent early widens at once, so an overload is answered before the window ends.
    /// No memory across windows: no wind-up (a leaky bucket swung theta x4 over 30 s cycles).
    fn account(&mut self, now: u32) {
        let budget = self.cfg.budget_bps as u64;
        let elapsed = now.wrapping_sub(self.window_start);
        if budget == 0 || elapsed >= u32::MAX / 2 {
            self.window_start = now;
            self.window_bits = 0;
            return;
        }
        let window = (CTRL_WINDOW_DATAGRAMS * CTRL_DATAGRAM_BITS * TICK_HZ as u64).div_ceil(budget).clamp((TICK_HZ / 2) as u64, CTRL_WINDOW_MAX as u64);
        let allowance = |ticks: u64| budget * ticks / TICK_HZ as u64;
        let over_early = self.window_bits > allowance(window);
        if !over_early && (elapsed as u64) < window {
            return;
        }
        if over_early || self.window_bits > allowance(elapsed as u64) {
            self.theta_scale *= 1.25;
        } else if self.window_bits < allowance(elapsed as u64) * 8 / 10 {
            self.theta_scale *= 0.9;
        }
        self.theta_scale = self.theta_scale.clamp(self.cfg.theta_scale_min, self.cfg.theta_scale_max);
        self.window_start = now;
        self.window_bits = 0;
    }
}

/// Entities per paced keyframe part: one part is at most `KF_PART_LINK_S` of link time at the
/// budget (header included), never fewer than one entity, never more than fit `MAX_DATAGRAM`, and
/// never so few that a keyframe needs more than 64 parts (the receiver's reconciliation mask).
fn part_entities(budget_bps: u32, n: usize) -> usize {
    let max = (MAX_DATAGRAM / ENTITY_BYTES).max(1);
    let link_bytes = (budget_bps as usize / 8) * KF_PART_LINK_S;
    let per = (link_bytes.saturating_sub(UDP_IP_OVERHEAD + KF_HEADER_BYTES) / ENTITY_BYTES).clamp(1, max);
    per.max(n.div_ceil(64))
}

/// Ticks until the next paced part: the link time of `wire_bytes` at the budget, at least one
/// ack interval. 0 at budget 0 (no pacing).
fn pace_ticks(wire_bytes: usize, budget_bps: u32) -> u32 {
    if budget_bps == 0 {
        return 0;
    }
    let t = (wire_bytes as u64 * 8 * TICK_HZ as u64).div_ceil(budget_bps as u64);
    (t.min(u32::MAX as u64 / 4) as u32).max(KF_PART_MIN_TICKS)
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
    use crate::classes::{CHAIR, PERSON};
    use crate::receiver::{Receiver, ReceiverConfig};

    fn edge() -> Edge {
        let mut e = Edge::new(1, 42, EdgeConfig::default());
        e.tick(&[], 0); // Hello
        e.on_datagram(&encode(&Message::Ack { last_seq: 0, missing: vec![], budget_bps: 0 }));
        e
    }

    fn ack(budget_bps: u32) -> Vec<u8> {
        encode(&Message::Ack { last_seq: 0, missing: vec![], budget_bps })
    }

    #[test]
    fn hello_is_refreshed_periodically_after_ack() {
        let mut e = edge();
        assert!(e.tick(&[], 1).is_empty());
        let out = e.tick(&[], 5 * TICK_HZ + 1);
        assert!(matches!(decode(&out[0]).unwrap(), Message::Hello { .. }));
        // ...beside the heartbeat keyframe, which goes out even when nothing is tracked.
        assert!(matches!(decode(&out[1]).unwrap(), Message::Keyframe { part: 0, of: 1, ref entities, .. } if entities.is_empty()));
        assert_eq!(out.len(), 2);
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
            Message::Delta { updates, theta_q: 15, .. } => assert!(matches!(updates[0], Update::Update(_))),
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

    #[test]
    fn budget_sets_cadence_and_zero_restores_the_config() {
        let cfg = EdgeConfig { keyframe_ticks: TICK_HZ, ..EdgeConfig::default() };
        let mut e = Edge::new(1, 42, cfg);
        assert_eq!((e.stats().keyframe_ticks, e.stats().pose_ticks, e.stats().budget_bps), (TICK_HZ, TICK_HZ / 2, 0));
        e.set_budget(600);
        let c = cadence(600);
        assert_eq!((e.stats().keyframe_ticks, e.stats().pose_ticks, e.stats().budget_bps), (c.keyframe_ticks, c.pose_ticks, 600));
        assert_eq!(e.cfg.hello_refresh_ticks, c.hello_refresh_ticks);
        // The server's Ack is authoritative, including 0 (unlimited).
        e.on_datagram(&ack(1500));
        assert_eq!(e.stats().keyframe_ticks, cadence(1500).keyframe_ticks);
        e.on_datagram(&ack(0));
        assert_eq!((e.stats().keyframe_ticks, e.cfg.hello_refresh_ticks, e.stats().pose_ticks), (TICK_HZ, 5 * TICK_HZ, TICK_HZ / 2));
        assert_eq!(e.cfg.t_max_ticks, 3 * TICK_HZ);
        e.on_datagram(&ack(8000));
        assert_eq!((e.stats().keyframe_ticks, e.cfg.t_max_ticks), (2 * TICK_HZ, 3 * TICK_HZ), "the defaults at >= 8 kbit/s");
        // A budget in the constructor config applies at once.
        let e = Edge::new(1, 42, EdgeConfig { budget_bps: 600, ..EdgeConfig::default() });
        assert_eq!(e.stats().keyframe_ticks, c.keyframe_ticks);
    }

    #[test]
    fn low_budget_slows_keyframes_and_hello() {
        let mut e = Edge::new(1, 42, EdgeConfig::default());
        e.tick(&[], 0);
        e.on_datagram(&ack(600));
        let c = cadence(600);
        let chair = Track { id: 2, class: CHAIR, pos: [1.0, 0.0, 1.0], vel: [0.0; 3], conf: 200 };
        let mut kf_ticks = Vec::new();
        let (mut hellos, mut deltas) = (0, 0);
        for now in 1..(60 * TICK_HZ) {
            for d in e.tick(&[chair], now) {
                match decode(&d).unwrap() {
                    Message::Keyframe { .. } => kf_ticks.push(now),
                    Message::Hello { .. } => hellos += 1,
                    _ => deltas += 1,
                }
            }
        }
        assert_eq!(deltas, 1, "the spawn only: the age cap stays behind the keyframe period");
        assert_eq!(e.cfg.t_max_ticks, c.keyframe_ticks * 3 / 2);
        assert_eq!(kf_ticks, vec![c.keyframe_ticks, 2 * c.keyframe_ticks, 3 * c.keyframe_ticks, 4 * c.keyframe_ticks]);
        assert_eq!(hellos, 2, "hello refresh every {} ticks", c.hello_refresh_ticks);
    }

    #[test]
    fn pose_is_gated_by_the_budget_cadence() {
        let mut e = edge();
        assert!(e.pose([0.0; 3], [0.0, 0.0, 0.0, 1.0], true, 10).is_some());
        assert!(e.pose([0.0; 3], [0.0, 0.0, 0.0, 1.0], true, 11).is_none());
        // A caller on its own 2 Hz clock may land a frame early; still due.
        assert!(e.pose([0.0; 3], [0.0, 0.0, 0.0, 1.0], true, 10 + TICK_HZ / 2 - 5).is_none());
        assert!(e.pose([0.0; 3], [0.0, 0.0, 0.0, 1.0], true, 10 + TICK_HZ / 2 - 4).is_some());
        e.on_datagram(&ack(600));
        let t = 10 + TICK_HZ / 2 - 4;
        assert!(e.pose([0.0; 3], [0.0, 0.0, 0.0, 1.0], true, t + 9 * TICK_HZ).is_none(), "10 s below 4 kbit/s");
        assert!(e.pose([0.0; 3], [0.0, 0.0, 0.0, 1.0], true, t + 10 * TICK_HZ).is_some());
    }

    /// One delta per tick (35 B payload): ~34 kbit/s of payload, ~61 kbit/s on the link. At a
    /// 45 kbit/s budget the payload alone is under the controller's 90 % target (it would narrow);
    /// only header accounting sees the overrun.
    #[test]
    fn controller_counts_the_udp_ip_header() {
        let budget = 45_000;
        let mut e = Edge::new(1, 42, EdgeConfig::default());
        let hello = e.tick(&[], 0);
        e.on_datagram(&ack(budget));
        let (mut payload, mut wire) = (hello[0].len(), hello[0].len() + UDP_IP_OVERHEAD);
        for now in 1..=2 * TICK_HZ {
            let jump = Track { id: 1, class: PERSON, pos: [0.5 * (now % 2) as f32, 0.0, 0.0], vel: [0.0; 3], conf: 200 };
            for d in e.tick(&[jump], now) {
                payload += d.len();
                wire += d.len() + UDP_IP_OVERHEAD;
            }
        }
        let (payload_bps, wire_bps) = (payload as u32 * 4, wire as u32 * 4); // two seconds
        assert!(payload_bps > budget * 7 / 10 && payload_bps < budget * 9 / 10, "payload alone: {payload_bps} bit/s");
        assert!(wire_bps > budget, "on the link: {wire_bps} bit/s");
        assert!(e.stats().theta_scale > 1.2, "controller saw the header: {}", e.stats().theta_scale);
        assert_eq!(e.stats().bytes_total, payload as u64, "bytes_total stays payload only");
    }

    /// `n` people circling at 1.2 m/s on 3 m circles (continuous turning: a delta every ~0.6 s each
    /// at θ 0.15), each in its own phase.
    fn circlers(n: u32, now: u32) -> Vec<Track> {
        (0..n)
            .map(|i| {
                let a = now as f64 / TICK_HZ as f64 * 0.4 + i as f64 * 1.3;
                let c = [i as f64 * 8.0, 0.0];
                let pos = [(c[0] + 3.0 * a.cos()) as f32, 0.0, (c[1] + 3.0 * a.sin()) as f32];
                let vel = [(-1.2 * a.sin()) as f32, 0.0, (1.2 * a.cos()) as f32];
                Track { id: 1 + i, class: PERSON, pos, vel, conf: 230 }
            })
            .collect()
    }

    /// The controller holds the budget on average without over-throttling: the long-run rate on the
    /// link sits at 75-100 % of the budget (measured 80-92 %) and the threshold settles instead of
    /// ratcheting to the max. (The fixed 0.5 s window read one 68 B datagram as 1088 bit/s and left
    /// a 1000 bit/s link at 38 %.)
    #[test]
    fn controller_holds_low_budgets_on_average() {
        for (budget, walkers) in [(450u32, 1u32), (1000, 2), (1500, 2), (8000, 12)] {
            let mut e = Edge::new(1, 42, EdgeConfig::default());
            e.tick(&[], 0);
            e.on_datagram(&ack(budget));
            let (warmup, end) = (60 * TICK_HZ, 240 * TICK_HZ);
            let (mut bits, mut scales) = (0u64, Vec::new());
            for now in 1..end {
                for d in e.tick(&circlers(walkers, now), now) {
                    if now >= warmup {
                        bits += ((d.len() + UDP_IP_OVERHEAD) * 8) as u64;
                    }
                }
                if now >= warmup && now % (TICK_HZ / 2) == 0 {
                    scales.push(e.stats().theta_scale);
                }
            }
            let bps = bits * TICK_HZ as u64 / (end - warmup) as u64;
            let mean_scale = scales.iter().sum::<f32>() / scales.len() as f32;
            let at_max = scales.iter().filter(|s| **s >= e.cfg.theta_scale_max).count();
            assert!(bps >= budget as u64 * 3 / 4 && bps <= budget as u64, "budget {budget}: {bps} bit/s, theta scale {mean_scale}");
            assert!(at_max == 0, "budget {budget}: theta at the max in {at_max} of {} samples", scales.len());
            assert!(mean_scale > e.cfg.theta_scale_min && mean_scale < e.cfg.theta_scale_max, "budget {budget}: regulating, not pinned ({mean_scale})");
        }
    }

    fn statics(n: u32) -> Vec<Track> {
        (0..n).map(|i| Track { id: 10 + i, class: CHAIR, pos: [i as f32, 0.0, 1.0], vel: [0.0; 3], conf: 200 }).collect()
    }

    #[test]
    fn keyframe_parts_are_paced_at_the_budget() {
        for budget in [600u32, 1500, 8000] {
            let mut e = Edge::new(1, 42, EdgeConfig::default());
            e.tick(&[], 0);
            e.on_datagram(&ack(budget));
            let tracks = statics(12);
            let kf_tick = cadence(budget).keyframe_ticks;
            let mut parts: Vec<(u32, usize, u8, u8, usize, u32)> = Vec::new(); // (now, wire, part, of, n, msg tick)
            for now in 1..kf_tick + 30 * TICK_HZ {
                let out = e.tick(&tracks, now);
                let n_kf = out.iter().filter(|d| matches!(decode(d), Ok(Message::Keyframe { .. }))).count();
                assert!(n_kf <= 1, "budget {budget}: {n_kf} parts in one tick at {now}");
                for d in out {
                    if let Ok(Message::Keyframe { part, of, entities, tick, .. }) = decode(&d) {
                        assert!(entities.iter().all(|s| s.tick == now), "parts carry state sampled when sent");
                        parts.push((now, d.len() + UDP_IP_OVERHEAD, part, of, entities.len(), tick));
                    }
                }
            }
            let of = parts[0].3 as usize;
            let first = &parts[..of];
            assert_eq!(first.iter().map(|p| p.4).sum::<usize>(), 12, "budget {budget}");
            assert!(first.iter().enumerate().all(|(i, p)| p.2 as usize == i && p.5 == kf_tick));
            for w in first.windows(2) {
                let link_ticks = (w[0].1 as u32 * 8 * TICK_HZ).div_ceil(budget).max(KF_PART_MIN_TICKS);
                assert_eq!(w[1].0 - w[0].0, link_ticks, "budget {budget}: one part per link time");
            }
            for p in first {
                assert!(p.4 == 1 || p.1 <= (budget / 8) as usize * KF_PART_LINK_S, "budget {budget}: part of {} B is over 2 s of budget", p.1);
            }
            match budget {
                600 => assert_eq!(of, 4, "3 entities per part at 600 bit/s"),
                1500 => assert_eq!(of, 2, "10 per part at 1500 bit/s"),
                _ => assert_eq!(of, 1),
            }
        }
    }

    /// The ghosts are the receiver's state: at a low budget (paced keyframes, deltas between
    /// parts, spawns and despawns while a keyframe is in flight) after every tick of a lossless
    /// link, the receiver holds exactly the ghosts.
    #[test]
    fn ghosts_stay_a_faithful_model_of_the_receiver_under_pacing() {
        for budget in [0u32, 600, 1500] {
            let mut e = Edge::new(1, 42, EdgeConfig::default());
            let mut rx = Receiver::new(ReceiverConfig::default());
            for d in e.tick(&[], 0) {
                rx.on_datagram(&d).unwrap();
            }
            e.on_datagram(&rx.make_ack(budget));
            for now in 1..(90 * TICK_HZ) {
                let t = now as f32 / TICK_HZ as f32;
                let mut tracks = statics(6);
                // Walkers come and go; one turns every few seconds.
                tracks.retain(|tr| !(now / (7 * TICK_HZ) + tr.id).is_multiple_of(4));
                let dir = if (now / (3 * TICK_HZ)).is_multiple_of(2) { 1.0 } else { -1.0 };
                tracks.push(Track { id: 1, class: PERSON, pos: [dir * (t % 3.0), 0.0, 0.0], vel: [dir, 0.0, 0.0], conf: 230 });
                if (now / (5 * TICK_HZ)).is_multiple_of(2) {
                    tracks.push(Track { id: 2, class: PERSON, pos: [0.0, 0.0, t % 4.0], vel: [0.0, 0.0, 1.0], conf: 180 });
                }
                for d in e.tick(&tracks, now) {
                    rx.on_datagram(&d).unwrap();
                }
                if now % 12 == 0 {
                    e.on_datagram(&rx.make_ack(budget));
                }
                let mut g: Vec<EntityState> = e.ghosts().to_vec();
                let mut r = rx.raw_entities();
                g.sort_by_key(|s| s.id);
                r.sort_by_key(|s| s.id);
                assert_eq!(g, r, "budget {budget}, tick {now}");
            }
            assert!(rx.stats().keyframes > 3, "budget {budget}");
        }
    }

    #[test]
    fn budget_lifted_mid_keyframe_sends_the_rest_at_once() {
        let mut e = Edge::new(1, 42, EdgeConfig::default());
        e.tick(&[], 0);
        e.on_datagram(&ack(600));
        let tracks = statics(10);
        let start = cadence(600).keyframe_ticks;
        for now in 1..start {
            e.tick(&tracks, now);
        }
        let out = e.tick(&tracks, start);
        assert!(matches!(decode(&out[0]).unwrap(), Message::Keyframe { part: 0, of: 4, .. }));
        e.on_datagram(&ack(0));
        let out = e.tick(&tracks, start + 1);
        let parts: Vec<u8> = out.iter().filter_map(|d| match decode(d) { Ok(Message::Keyframe { part, .. }) => Some(part), _ => None }).collect();
        assert_eq!(parts, vec![1, 2, 3]);
    }
}
