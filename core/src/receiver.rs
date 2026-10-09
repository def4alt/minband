//! Twin-side sync for one device: apply datagrams, detect gaps, produce acks, extrapolate.

use crate::cadence::{cadence, Cadence};
use crate::classes::prior;
use crate::predictor::Predictor;
use crate::wire::{decode, encode, newest_tick, theta_m, CodecError, EntityState, Message, Update};
use crate::TICK_HZ;

/// Thresholds at budget 0 (unlimited). At any other advertised budget the coast, stale and drop
/// thresholds come from [`cadence`] instead, and the hard limit is at least 3 x the drop threshold.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ReceiverConfig {
    /// Entity not refreshed for this long is reported stale.
    pub stale_ticks: u32,
    /// Entity not refreshed for this long is dropped by `gc`.
    pub drop_ticks: u32,
    /// A seq gap that stays open for this long (edge time) is nacked.
    pub gap_nack_ticks: u32,
    /// Forget a gap after this long; it can no longer be repaired meaningfully.
    pub gap_forget_ticks: u32,
    /// Re-nack an open gap at most this often (one round trip plus margin).
    pub renack_ticks: u32,
    /// Drop an entity by age regardless of device liveness after this long.
    pub hard_drop_ticks: u32,
    /// Device silent this long: its entities are coasting (one keyframe period plus margin).
    pub coast_ticks: u32,
}

impl Default for ReceiverConfig {
    fn default() -> Self {
        Self {
            stale_ticks: 6 * TICK_HZ,
            drop_ticks: 10 * TICK_HZ,
            gap_nack_ticks: TICK_HZ / 5,
            gap_forget_ticks: 3 * TICK_HZ,
            renack_ticks: TICK_HZ / 2,
            hard_drop_ticks: 30 * TICK_HZ,
            coast_ticks: 2 * TICK_HZ + TICK_HZ / 2,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct ReceiverStats {
    pub datagrams: u32,
    pub bytes: u64,
    pub deltas: u32,
    pub keyframes: u32,
    pub poses: u32,
    pub gaps_detected: u32,
    pub nacks_sent: u32,
    pub out_of_order_dropped: u32,
    /// Entities removed because a complete keyframe no longer listed them.
    pub reconciled: u32,
}

/// Cap on the error radius while coasting: beyond this it says nothing a consumer can use.
pub const CE_MAX_M: f32 = 1000.0;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Extrapolated {
    pub state: EntityState,
    /// Ticks since the entity was last refreshed by the edge.
    pub age_ticks: u32,
    pub stale: bool,
    /// Position threshold (m) the edge declared in the datagram that last refreshed this entity:
    /// while the link is good the twin is within this of the edge's track.
    pub theta: f32,
    /// `theta` no longer bounds the error: the device has been silent for at least `coast_ticks`
    /// (the heartbeat that makes "no update = within threshold" true is missing), or it spoke again
    /// after such a silence but nothing sent since has refreshed this entity (updates lost in the
    /// blackout are repaired a round trip later, or by the next keyframe).
    pub coasting: bool,
    /// Honest error radius (m): `theta`, or while coasting `theta + max_speed(class) x silence`,
    /// silence counted from the device's last datagram before the (first unrepaired) blackout, so a
    /// missed heartbeat is a visible jump (capped at `CE_MAX_M`).
    pub ce: f32,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Pose {
    pub pos: [f32; 3],
    pub quat: [f32; 4],
    pub origin_locked: bool,
    pub tick: u32,
}

/// What a datagram did, for logging and the viewer's event feed.
#[derive(Clone, Debug, PartialEq)]
pub enum Event {
    Hello { device_id: u32, session_nonce: u32 },
    Delta { seq: u32, applied: usize },
    Keyframe { seq: u32, part: u8, of: u8, entities: usize },
    Pose { seq: u32 },
    Bye { seq: u32 },
    Ignored,
}

/// Effective liveness thresholds.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Limits {
    coast: u32,
    stale: u32,
    drop: u32,
    hard: u32,
}

impl Limits {
    fn max(self, o: Limits) -> Limits {
        Limits { coast: self.coast.max(o.coast), stale: self.stale.max(o.stale), drop: self.drop.max(o.drop), hard: self.hard.max(o.hard) }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct Entity {
    state: EntityState,
    /// `theta_q` of the datagram that last refreshed this entity.
    theta_q: u8,
    /// Not refreshed since the device resumed after a silence of at least `coast_ticks`: the edge
    /// tick of its last datagram before that silence (the oldest, if several blackouts went by).
    suspect_since: Option<u32>,
}

pub struct Receiver {
    cfg: ReceiverConfig,
    entities: Vec<Entity>,
    last_seq: Option<u32>,
    /// Open gaps: (seq, edge tick when noticed, edge tick of the last nack).
    gaps: Vec<(u32, u32, Option<u32>)>,
    /// Keyframe parts seen for the keyframe at `tick`: (tick, of, part mask, ids).
    keyframe: Option<(u32, u8, u64, Vec<u32>)>,
    /// Newest edge tick seen in any datagram: the edge time of its last datagram.
    last_edge_tick: u32,
    /// Send tick of the first datagram after the last silence of at least `coast_ticks`.
    resumed_at: Option<u32>,
    device_id: Option<u32>,
    session_nonce: Option<u32>,
    pose: Option<Pose>,
    /// Budget last advertised in `make_ack`; the edge derives its cadence from the same number.
    budget_bps: u32,
    /// After a change to tighter limits, the previous ones still apply until this edge tick: the
    /// edge has not heard of the new budget yet, so one more old-cadence silence is normal.
    grace: Option<(Limits, u32)>,
    stats: ReceiverStats,
}

impl Receiver {
    pub fn new(cfg: ReceiverConfig) -> Self {
        Self {
            cfg,
            entities: Vec::new(),
            last_seq: None,
            gaps: Vec::new(),
            keyframe: None,
            last_edge_tick: 0,
            resumed_at: None,
            device_id: None,
            session_nonce: None,
            pose: None,
            budget_bps: 0,
            grace: None,
            stats: ReceiverStats::default(),
        }
    }

    pub fn stats(&self) -> ReceiverStats { self.stats }
    pub fn device_id(&self) -> Option<u32> { self.device_id }
    pub fn pose(&self) -> Option<Pose> { self.pose }
    /// Latest edge tick seen in any message; the host maps this to its own clock.
    pub fn last_edge_tick(&self) -> u32 { self.last_edge_tick }
    /// Budget last advertised in `make_ack` (0 = unlimited, also before the first ack).
    pub fn budget_bps(&self) -> u32 { self.budget_bps }
    /// The cadence of the advertised budget (what the edge is expected to follow).
    pub fn cadence(&self) -> Cadence { cadence(self.budget_bps) }
    pub fn needs_ack(&self) -> bool {
        let now = self.last_edge_tick;
        self.gaps.iter().any(|(_, since, last)| self.nack_due(now, *since, *last))
    }

    /// The device has sent nothing for `coast_ticks` (of its advertised budget) at `at_tick`.
    pub fn coasting(&self, at_tick: u32) -> bool {
        since(at_tick, self.last_edge_tick) >= self.limits(at_tick).coast
    }

    fn nack_due(&self, now: u32, since: u32, last: Option<u32>) -> bool {
        now.wrapping_sub(since) >= self.cfg.gap_nack_ticks
            && match last { None => true, Some(t) => now.wrapping_sub(t) >= self.cfg.renack_ticks }
    }

    fn limits_for(&self, budget_bps: u32) -> Limits {
        if budget_bps == 0 {
            let c = &self.cfg;
            return Limits { coast: c.coast_ticks, stale: c.stale_ticks, drop: c.drop_ticks, hard: c.hard_drop_ticks };
        }
        let c = cadence(budget_bps);
        Limits { coast: c.coast_ticks, stale: c.stale_ticks, drop: c.drop_ticks, hard: self.cfg.hard_drop_ticks.max(3 * c.drop_ticks) }
    }

    fn limits(&self, at_tick: u32) -> Limits {
        let l = self.limits_for(self.budget_bps);
        match self.grace {
            Some((prev, until)) if since(until, at_tick) > 0 => l.max(prev),
            _ => l,
        }
    }

    pub fn on_datagram(&mut self, bytes: &[u8]) -> Result<Event, CodecError> {
        let msg = decode(bytes)?;
        let sent = match &msg {
            Message::Hello { tick, .. } | Message::Delta { tick, .. } | Message::Pose { tick, .. } | Message::Bye { tick, .. } => Some(*tick),
            Message::Keyframe { tick, entities, .. } => Some(newest_tick(*tick, entities)),
            Message::Ack { .. } => None,
        };
        if let Some(t) = sent {
            if self.stats.datagrams > 0 {
                self.note_resume(t);
            }
        }
        self.stats.datagrams += 1;
        self.stats.bytes += bytes.len() as u64;
        Ok(match msg {
            Message::Hello { device_id, session_nonce, tick, .. } => {
                if self.session_nonce.is_some() && self.session_nonce != Some(session_nonce) {
                    // New session from this device: reset everything. A first Hello on a receiver
                    // that already adopted the device (server restart) only records the nonce.
                    self.entities.clear();
                    self.gaps.clear();
                    self.keyframe = None;
                    self.last_seq = None;
                    self.last_edge_tick = 0;
                    self.resumed_at = None;
                    self.grace = None;
                }
                self.session_nonce = Some(session_nonce);
                self.device_id = Some(device_id);
                self.note_tick(tick);
                Event::Hello { device_id, session_nonce }
            }
            Message::Delta { seq, tick, theta_q, updates } => {
                self.note_tick(tick);
                if !self.note_seq(seq) {
                    return Ok(Event::Ignored);
                }
                self.stats.deltas += 1;
                let mut applied = 0;
                for u in updates {
                    if self.apply(u, theta_q) {
                        applied += 1;
                    }
                }
                Event::Delta { seq, applied }
            }
            Message::Keyframe { seq, tick, theta_q, part, of, entities } => {
                // A paced part sent after the keyframe's tick carries entities sampled at its send
                // tick, so the newest tick it holds is when the device last spoke.
                self.note_tick(newest_tick(tick, &entities));
                if !self.note_seq(seq) {
                    return Ok(Event::Ignored);
                }
                self.stats.keyframes += 1;
                let n = entities.len();
                let ids: Vec<u32> = entities.iter().map(|e| e.id).collect();
                for e in entities {
                    self.apply(Update::Update(e), theta_q);
                }
                self.reconcile_keyframe(tick, part, of, ids);
                Event::Keyframe { seq, part, of, entities: n }
            }
            Message::Pose { seq, tick, pos, quat, origin_locked } => {
                self.note_tick(tick);
                self.note_seq(seq);
                self.stats.poses += 1;
                self.pose = Some(Pose { pos, quat, origin_locked, tick });
                Event::Pose { seq }
            }
            Message::Bye { seq, tick } => {
                self.note_tick(tick);
                self.note_seq(seq);
                self.entities.clear();
                Event::Bye { seq }
            }
            Message::Ack { .. } => Event::Ignored,
        })
    }

    /// Build an ack datagram. Call every ~100 ms or when `needs_ack()`. `budget_bps` is pushed to
    /// the edge, which derives its keyframe/hello/pose cadence from it; this receiver derives its
    /// coast/stale/drop thresholds from the same number.
    pub fn make_ack(&mut self, budget_bps: u32) -> Vec<u8> {
        let now = self.last_edge_tick;
        if budget_bps != self.budget_bps {
            let before = self.limits(now);
            self.budget_bps = budget_bps;
            self.grace = Some((before, now.wrapping_add(before.coast)));
        }
        self.gaps.retain(|(_, since, _)| now.wrapping_sub(*since) < self.cfg.gap_forget_ticks);
        let mut missing = Vec::new();
        let (gap_nack, renack) = (self.cfg.gap_nack_ticks, self.cfg.renack_ticks);
        for (seq, since, last) in self.gaps.iter_mut() {
            let due = now.wrapping_sub(*since) >= gap_nack
                && match *last { None => true, Some(t) => now.wrapping_sub(t) >= renack };
            if due && missing.len() < 32 {
                missing.push(*seq);
                *last = Some(now);
                self.stats.nacks_sent += 1;
            }
        }
        encode(&Message::Ack { last_seq: self.last_seq.unwrap_or(0), missing, budget_bps })
    }

    /// Entities extrapolated to `at_tick` (edge clock).
    pub fn extrapolate(&self, at_tick: u32) -> Vec<Extrapolated> {
        let l = self.limits(at_tick);
        let device_coasting = since(at_tick, self.last_edge_tick) >= l.coast;
        self.entities
            .iter()
            .map(|e| {
                let age = since(at_tick, e.state.tick);
                let theta = theta_m(e.theta_q);
                // Trust runs out at the oldest of: the device's last word (while it is silent) and
                // the start of a blackout this entity has not been refreshed since.
                let from = match (device_coasting, e.suspect_since) {
                    (_, Some(s)) => Some(s),
                    (true, None) => Some(self.last_edge_tick),
                    (false, None) => None,
                };
                let ce = match from {
                    Some(f) => {
                        let r = theta + prior(e.state.class).max_speed * (since(at_tick, f) as f32 / TICK_HZ as f32);
                        if r < CE_MAX_M { r } else { CE_MAX_M }
                    }
                    None => theta,
                };
                Extrapolated { state: Predictor::step(&e.state, at_tick), age_ticks: age, stale: age >= l.stale, theta, coasting: from.is_some(), ce }
            })
            .collect()
    }

    /// Drop stale entities. While the device is alive, removal is driven by Despawns and
    /// keyframe reconciliation; age-based dropping applies when the device has gone silent
    /// (`stale_ticks`) or unconditionally after `hard_drop_ticks`. Returns dropped ids.
    pub fn gc(&mut self, at_tick: u32) -> Vec<u32> {
        let l = self.limits(at_tick);
        let device_silent = since(at_tick, self.last_edge_tick) >= l.stale;
        let mut dropped = Vec::new();
        self.entities.retain(|e| {
            let age = since(at_tick, e.state.tick);
            if (device_silent && age >= l.drop) || age >= l.hard {
                dropped.push(e.state.id);
                false
            } else {
                true
            }
        });
        if matches!(self.grace, Some((_, until)) if since(until, at_tick) == 0) {
            self.grace = None;
        }
        dropped
    }

    pub fn raw_entities(&self) -> Vec<EntityState> { self.entities.iter().map(|e| e.state).collect() }

    /// Once every part of a keyframe has arrived, entities it does not list (and that were not
    /// observed after its tick) are gone: this repairs lost Despawns without waiting for gc. Parts
    /// may arrive over time (paced) with deltas in between; a part of an older keyframe never
    /// discards the progress of a newer one.
    fn reconcile_keyframe(&mut self, tick: u32, part: u8, of: u8, ids: Vec<u32>) {
        if of == 0 || of > 64 {
            return;
        }
        let (mask, mut all_ids) = match self.keyframe.take() {
            Some((t, o, m, v)) if t == tick && o == of => (m, v),
            Some(k) if tick.wrapping_sub(k.0) >= u32::MAX / 2 => {
                self.keyframe = Some(k); // late part of an older keyframe
                return;
            }
            _ => (0u64, Vec::new()),
        };
        let mask = mask | (1u64 << part.min(63));
        all_ids.extend(ids);
        let complete = of == 64 || mask == (1u64 << of) - 1;
        if !complete {
            self.keyframe = Some((tick, of, mask, all_ids));
            return;
        }
        let before = self.entities.len();
        self.entities.retain(|e| all_ids.contains(&e.state.id) || tick.wrapping_sub(e.state.tick) >= u32::MAX / 2);
        self.stats.reconciled += (before - self.entities.len()) as u32;
        // A whole keyframe taken after the resume re-establishes every entity that remains.
        if matches!(self.resumed_at, Some(r) if tick.wrapping_sub(r) < u32::MAX / 2) {
            self.resumed_at = None;
            for e in &mut self.entities {
                e.suspect_since = None;
            }
        }
    }

    /// A datagram sent at `sent` after a silence of at least `coast_ticks`: until something sent
    /// from now on refreshes them, the entities held so far are as uncertain as during the silence.
    fn note_resume(&mut self, sent: u32) {
        let silence = sent.wrapping_sub(self.last_edge_tick);
        if silence >= u32::MAX / 2 || silence < self.limits(sent).coast {
            return;
        }
        let start = self.last_edge_tick;
        for e in &mut self.entities {
            e.suspect_since.get_or_insert(start);
        }
        self.resumed_at = Some(sent);
    }

    fn note_tick(&mut self, tick: u32) {
        if tick.wrapping_sub(self.last_edge_tick) < u32::MAX / 2 {
            self.last_edge_tick = tick;
        }
    }

    /// Returns false if this seq was already seen (duplicate) or is a stale repair of a forgotten gap.
    fn note_seq(&mut self, seq: u32) -> bool {
        match self.last_seq {
            None => {
                self.last_seq = Some(seq);
                true
            }
            Some(last) => {
                let ahead = seq.wrapping_sub(last);
                if ahead == 0 {
                    false
                } else if ahead < u32::MAX / 2 {
                    for g in 1..ahead {
                        if self.gaps.len() < 256 {
                            self.gaps.push((last.wrapping_add(g), self.last_edge_tick, None));
                            self.stats.gaps_detected += 1;
                        }
                    }
                    self.last_seq = Some(seq);
                    true
                } else {
                    // Late arrival: fills a gap if we still track it.
                    let before = self.gaps.len();
                    self.gaps.retain(|(s, _, _)| *s != seq);
                    if self.gaps.len() == before {
                        self.stats.out_of_order_dropped += 1;
                        false
                    } else {
                        true
                    }
                }
            }
        }
    }

    /// Apply one update; newer observation wins. Returns true if state changed.
    fn apply(&mut self, u: Update, theta_q: u8) -> bool {
        match u {
            Update::Spawn(s) | Update::Update(s) => {
                match self.entities.iter_mut().find(|e| e.state.id == s.id) {
                    Some(e) => {
                        if s.tick.wrapping_sub(e.state.tick) < u32::MAX / 2 {
                            *e = Entity { state: s, theta_q, suspect_since: None };
                            true
                        } else {
                            false
                        }
                    }
                    None => {
                        self.entities.push(Entity { state: s, theta_q, suspect_since: None });
                        true
                    }
                }
            }
            Update::Despawn { id, tick } => {
                let before = self.entities.len();
                self.entities.retain(|e| !(e.state.id == id && tick.wrapping_sub(e.state.tick) < u32::MAX / 2));
                self.entities.len() != before
            }
        }
    }
}

/// JSON array of `{id,class,pos,vel,conf,tick,age,stale,theta,coasting,ce}` (metres, ticks).
pub fn extrapolated_json(items: &[Extrapolated]) -> String {
    let mut s = String::from("[");
    for (i, e) in items.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        let st = e.state;
        s.push_str(&format!(
            "{{\"id\":{},\"class\":{},\"pos\":[{},{},{}],\"vel\":[{},{},{}],\"conf\":{},\"tick\":{},\"age\":{},\"stale\":{},\"theta\":{},\"coasting\":{},\"ce\":{}}}",
            st.id, st.class, st.pos[0], st.pos[1], st.pos[2], st.vel[0], st.vel[1], st.vel[2], st.conf, st.tick, e.age_ticks, e.stale,
            e.theta, e.coasting, e.ce
        ));
    }
    s.push(']');
    s
}

/// Ticks from `t` to `at`, 0 if `at` is before `t` (the host's estimate of edge now can lag the
/// newest datagram by a tick or two; a wrapped difference would read as decades of silence).
fn since(at: u32, t: u32) -> u32 {
    let d = at.wrapping_sub(t);
    if d < u32::MAX / 2 { d } else { 0 }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::classes::{CHAIR, PERSON};
    use crate::wire::theta_q;

    fn st(id: u32, x: f32, tick: u32) -> EntityState {
        EntityState { id, class: PERSON, pos: [x, 0.0, 0.0], vel: [1.0, 0.0, 0.0], conf: 200, tick }
    }

    fn delta(seq: u32, tick: u32, updates: Vec<Update>) -> Vec<u8> {
        encode(&Message::Delta { seq, tick, theta_q: 15, updates })
    }

    fn kf(seq: u32, tick: u32, part: u8, of: u8, entities: Vec<EntityState>) -> Vec<u8> {
        encode(&Message::Keyframe { seq, tick, theta_q: 15, part, of, entities })
    }

    #[test]
    fn applies_and_extrapolates() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 0, vec![Update::Spawn(st(1, 0.0, 0))])).unwrap();
        let ex = r.extrapolate(TICK_HZ);
        assert_eq!(ex.len(), 1);
        assert!(ex[0].state.pos[0] > 0.9);
        assert!(!ex[0].stale);
        assert!(r.extrapolate(7 * TICK_HZ)[0].stale);
        assert_eq!(r.gc(11 * TICK_HZ), vec![1]);
    }

    #[test]
    fn detects_gap_and_nacks_after_delay() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 0, vec![])).unwrap();
        r.on_datagram(&delta(3, 10, vec![])).unwrap();
        assert_eq!(r.stats().gaps_detected, 1);
        assert!(!r.needs_ack());
        r.on_datagram(&delta(4, 40, vec![])).unwrap();
        assert!(r.needs_ack());
        let ack = r.make_ack(0);
        match decode(&ack).unwrap() {
            Message::Ack { last_seq: 4, missing, .. } => assert_eq!(missing, vec![2]),
            m => panic!("{m:?}"),
        }
        // Late arrival of seq 2 closes the gap.
        assert_ne!(r.on_datagram(&delta(2, 5, vec![])).unwrap(), Event::Ignored);
        assert!(!r.needs_ack());
    }

    #[test]
    fn gap_is_renacked_only_after_renack_interval() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 0, vec![])).unwrap();
        r.on_datagram(&delta(3, 40, vec![])).unwrap(); // gap: seq 2, since tick 40
        r.on_datagram(&delta(4, 70, vec![])).unwrap();
        assert!(r.needs_ack());
        r.make_ack(0);
        assert!(!r.needs_ack(), "just nacked");
        r.on_datagram(&delta(5, 80, vec![])).unwrap();
        match decode(&r.make_ack(0)).unwrap() {
            Message::Ack { missing, .. } => assert!(missing.is_empty(), "no repeat nack within renack_ticks"),
            m => panic!("{m:?}"),
        }
        assert_eq!(r.stats().nacks_sent, 1);
        r.on_datagram(&delta(6, 70 + TICK_HZ, vec![])).unwrap();
        assert!(r.needs_ack(), "renack after the interval");
        match decode(&r.make_ack(0)).unwrap() {
            Message::Ack { missing, .. } => assert_eq!(missing, vec![2]),
            m => panic!("{m:?}"),
        }
        assert_eq!(r.stats().nacks_sent, 2);
    }

    #[test]
    fn complete_keyframe_removes_unlisted_entities() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 10, vec![Update::Spawn(st(1, 0.0, 10)), Update::Spawn(st(2, 1.0, 10))])).unwrap();
        // Despawn of 2 at seq 2 is lost. Keyframe in two parts at tick 100 lists only 1 and 3.
        r.on_datagram(&kf(3, 100, 0, 2, vec![st(1, 0.5, 100)])).unwrap();
        assert_eq!(r.raw_entities().len(), 2, "incomplete keyframe must not remove anything");
        // An entity observed after the keyframe tick survives reconciliation.
        r.on_datagram(&delta(4, 101, vec![Update::Spawn(st(9, 0.0, 101))])).unwrap();
        r.on_datagram(&kf(5, 100, 1, 2, vec![st(3, 2.0, 100)])).unwrap();
        let ids: Vec<u32> = r.raw_entities().iter().map(|e| e.id).collect();
        assert_eq!(ids, vec![1, 9, 3]);
        assert_eq!(r.stats().reconciled, 1);
    }

    #[test]
    fn paced_keyframe_parts_reconcile_and_never_regress() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 10, vec![Update::Spawn(st(1, 0.0, 10)), Update::Spawn(st(2, 1.0, 10)), Update::Spawn(st(7, 9.0, 10))])).unwrap();
        // Keyframe taken at tick 100 lists 1, 2 (7 vanished, its Despawn lost); part 0 now.
        r.on_datagram(&kf(2, 100, 0, 2, vec![st(1, 0.5, 100)])).unwrap();
        // A delta between the parts moves 2 at tick 130.
        r.on_datagram(&delta(3, 130, vec![Update::Update(st(2, 5.0, 130))])).unwrap();
        // Part 1 goes out at tick 160 with 2 sampled then: newer than the delta, applied.
        r.on_datagram(&kf(4, 100, 1, 2, vec![st(2, 6.0, 160)])).unwrap();
        assert_eq!(r.last_edge_tick(), 160, "a paced part's send tick counts as the device speaking");
        let ids: Vec<u32> = r.raw_entities().iter().map(|e| e.id).collect();
        assert_eq!(ids, vec![1, 2], "7 reconciled once both parts arrived");
        assert_eq!(r.raw_entities()[1].pos[0], 6.0);
        // A late part of an older keyframe never moves an entity back nor resets a newer keyframe.
        r.on_datagram(&kf(6, 200, 0, 2, vec![st(1, 0.7, 200)])).unwrap();
        r.on_datagram(&kf(5, 100, 1, 2, vec![st(2, 0.0, 150)])).unwrap();
        assert_eq!(r.raw_entities()[1].pos[0], 6.0);
        r.on_datagram(&kf(7, 200, 1, 2, vec![st(2, 6.5, 230)])).unwrap();
        assert_eq!(r.raw_entities().len(), 2);
        assert_eq!(r.raw_entities()[1].pos[0], 6.5);
    }

    #[test]
    fn gc_waits_for_device_silence_unless_hard_limit() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 0, vec![Update::Spawn(st(1, 0.0, 0))])).unwrap();
        // Device keeps talking (poses) but the entity is never refreshed.
        r.on_datagram(&encode(&Message::Pose { seq: 2, tick: 11 * TICK_HZ, pos: [0.0; 3], quat: [0.0, 0.0, 0.0, 1.0], origin_locked: true })).unwrap();
        assert!(r.gc(11 * TICK_HZ).is_empty(), "device alive: keep until keyframe reconciliation");
        assert_eq!(r.gc(31 * TICK_HZ), vec![1], "hard limit");
    }

    #[test]
    fn older_observation_never_overwrites_newer() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 100, vec![Update::Spawn(st(1, 5.0, 100))])).unwrap();
        // A keyframe that was delayed (lower seq, older tick) must not move the entity back.
        r.on_datagram(&delta(3, 101, vec![])).unwrap();
        r.on_datagram(&kf(2, 50, 0, 1, vec![st(1, 0.0, 50)])).unwrap();
        assert_eq!(r.raw_entities()[0].pos[0], 5.0);
    }

    #[test]
    fn theta_follows_the_refreshing_datagram() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&encode(&Message::Delta { seq: 1, tick: 10, theta_q: theta_q(0.4), updates: vec![Update::Spawn(st(1, 0.0, 10))] })).unwrap();
        assert_eq!(r.extrapolate(10)[0].theta, 0.4);
        // An older state with another theta is rejected and does not change the declared theta.
        r.on_datagram(&encode(&Message::Keyframe { seq: 2, tick: 5, theta_q: theta_q(0.05), part: 0, of: 1, entities: vec![st(1, 0.0, 5)] })).unwrap();
        assert_eq!(r.extrapolate(10)[0].theta, 0.4);
        r.on_datagram(&encode(&Message::Delta { seq: 3, tick: 20, theta_q: theta_q(0.05), updates: vec![Update::Update(st(1, 0.1, 20))] })).unwrap();
        let e = r.extrapolate(20)[0];
        assert_eq!((e.theta, e.ce, e.coasting), (0.05, 0.05, false));
    }

    #[test]
    fn coasting_and_error_radius() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 0, vec![Update::Spawn(st(1, 0.0, 0)), Update::Spawn(EntityState { class: CHAIR, ..st(2, 3.0, 0) })])).unwrap();
        let coast = ReceiverConfig::default().coast_ticks;
        assert!(!r.coasting(coast - 1));
        assert!(r.extrapolate(coast - 1).iter().all(|e| !e.coasting && e.ce == 0.15));
        assert!(r.coasting(coast));
        let ex = r.extrapolate(coast);
        // Missed heartbeat: a visible jump, theta + max_speed x silence since the last datagram.
        assert!(ex[0].coasting && ex[0].ce == 0.15 + 3.0 * 2.5, "person: {}", ex[0].ce);
        assert!(ex[1].ce == 0.15 + 1.0 * 2.5, "chair: {}", ex[1].ce);
        let v: serde_json::Value = serde_json::from_str(&extrapolated_json(&ex)).unwrap();
        assert_eq!(v[0]["theta"], 0.15, "shortest f32 repr");
        assert_eq!((v[0]["coasting"].as_bool(), v[1]["ce"].as_f64()), (Some(true), Some(2.65)));
        assert_eq!((v[1]["class"].as_u64(), v[1]["age"].as_u64(), v[1]["stale"].as_bool()), (Some(CHAIR as u64), Some(300), Some(false)));
        assert_eq!(r.extrapolate(u32::MAX / 4)[0].ce, CE_MAX_M);
        // Any datagram ends coasting, even one that refreshes nothing.
        r.on_datagram(&encode(&Message::Hello { device_id: 1, session_nonce: 1, caps: 0, tick: coast })).unwrap();
        assert!(!r.coasting(coast + 1));
        // The host's estimate of edge now lagging the newest datagram is not silence.
        assert!(!r.coasting(coast - 5) && r.gc(coast - 5).is_empty() && !r.extrapolate(coast - 5)[0].stale);
    }

    /// After a blackout, trust comes back per entity: the first datagram ends the device's silence
    /// but vouches only for what it carries; the rest stays coasting, its ce still growing from the
    /// start of the blackout, until a datagram sent after the resume refreshes it.
    #[test]
    fn trust_returns_per_entity_after_a_blackout() {
        let mut r = Receiver::new(ReceiverConfig::default());
        let chair = |id, t| EntityState { class: CHAIR, vel: [0.0; 3], ..st(id, 3.0, t) };
        r.on_datagram(&delta(1, 100, vec![Update::Spawn(st(1, 0.0, 100)), Update::Spawn(st(2, 1.0, 100)), Update::Spawn(chair(3, 100))])).unwrap();
        let back = 100 + 10 * TICK_HZ; // 10 s blackout
        assert!(r.coasting(back - 1) && r.extrapolate(back - 1).iter().all(|e| e.coasting));
        // The first datagram after it refreshes only entity 1 (its other updates were lost).
        r.on_datagram(&delta(9, back, vec![Update::Update(st(1, 2.0, back))])).unwrap();
        let at = back + 30;
        assert!(!r.coasting(at), "the device is back");
        let ex = r.extrapolate(at);
        assert_eq!((ex[0].coasting, ex[0].ce), (false, 0.15));
        let silence_s = (at - 100) as f32 / TICK_HZ as f32;
        assert!(ex[1].coasting && ex[1].ce == 0.15 + 3.0 * silence_s, "walker 2 still unknown: {}", ex[1].ce);
        assert!(ex[2].coasting && ex[2].ce == 0.15 + 1.0 * silence_s, "chair: {}", ex[2].ce);
        // A spawn after the resume is trusted at once; an older state does not vouch for anything.
        r.on_datagram(&delta(10, back + 40, vec![Update::Spawn(st(7, 0.0, back + 40)), Update::Update(st(2, 1.0, 90))])).unwrap();
        let ex = r.extrapolate(back + 50);
        assert!(!ex[3].coasting && ex[1].coasting);
        // A repair (a delta update sent after the resume) restores entity 2.
        r.on_datagram(&delta(11, back + 60, vec![Update::Update(st(2, 1.5, back + 60))])).unwrap();
        let ex = r.extrapolate(back + 70);
        assert_eq!((ex[1].coasting, ex[1].ce), (false, 0.15));
        assert!(ex[2].coasting, "the chair waits for a keyframe");
        // A second blackout before the chair is refreshed: its silence still counts from the first.
        let back2 = back + 70 + 5 * TICK_HZ;
        r.on_datagram(&delta(12, back2, vec![Update::Update(st(1, 2.0, back2))])).unwrap();
        let ex = r.extrapolate(back2);
        assert!(ex[2].ce == 0.15 + 1.0 * ((back2 - 100) as f32 / TICK_HZ as f32), "chair from the first blackout: {}", ex[2].ce);
        assert!(ex[1].coasting && ex[1].ce == 0.15 + 3.0 * ((back2 - back - 60) as f32 / TICK_HZ as f32), "walker 2 from its repair");
        // A complete keyframe taken after the resume re-establishes everything that remains.
        r.on_datagram(&kf(13, back2 + 10, 0, 1, vec![st(1, 2.1, back2 + 10), st(2, 1.6, back2 + 10), chair(3, back2 + 10), st(7, 0.1, back2 + 10)])).unwrap();
        assert!(r.extrapolate(back2 + 11).iter().all(|e| !e.coasting && e.ce == 0.15));
    }

    /// A paced keyframe that started before the blackout and completes after it does not vouch
    /// for the entities its earlier parts carried; one taken after the resume does.
    #[test]
    fn only_a_keyframe_taken_after_the_resume_clears_trust() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 100, vec![Update::Spawn(st(1, 0.0, 100)), Update::Spawn(st(2, 1.0, 100))])).unwrap();
        r.on_datagram(&kf(2, 200, 0, 2, vec![st(1, 0.5, 200)])).unwrap();
        let back = 200 + 5 * TICK_HZ;
        r.on_datagram(&kf(3, 200, 1, 2, vec![st(2, 1.5, back)])).unwrap();
        let ex = r.extrapolate(back + 1);
        assert!(ex[0].coasting && !ex[1].coasting, "part 0 was sent before the blackout");
        // The resume datagram is itself a keyframe taken after the silence: all trusted at once.
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 100, vec![Update::Spawn(st(1, 0.0, 100)), Update::Spawn(st(2, 1.0, 100))])).unwrap();
        r.on_datagram(&kf(5, back, 0, 1, vec![st(1, 0.5, back), st(2, 1.5, back)])).unwrap();
        assert!(r.extrapolate(back + 1).iter().all(|e| !e.coasting && e.ce == 0.15));
        // Silence shorter than coast_ticks is not a blackout.
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 100, vec![Update::Spawn(st(1, 0.0, 100)), Update::Spawn(st(2, 1.0, 100))])).unwrap();
        r.on_datagram(&delta(2, 100 + ReceiverConfig::default().coast_ticks - 1, vec![])).unwrap();
        assert!(r.extrapolate(400).iter().all(|e| !e.coasting));
    }

    /// A static scene at 600 bit/s: keyframes every ~14 s are the only refresh. Between them the
    /// twin must not coast, go stale or drop anything; once the link dies it coasts after one
    /// missed keyframe, goes stale, and is dropped only after the cadence-derived limits.
    #[test]
    fn low_budget_static_scene_stays_fresh_between_keyframes() {
        let c = cadence(600);
        let mut r = Receiver::new(ReceiverConfig::default());
        r.make_ack(600);
        assert_eq!(r.cadence(), c);
        let mut seq = 0;
        let mut kf_at = |r: &mut Receiver, t: u32| {
            seq += 1;
            r.on_datagram(&encode(&Message::Keyframe { seq, tick: t, theta_q: 15, part: 0, of: 1, entities: vec![st(1, 0.0, t), EntityState { class: CHAIR, vel: [0.0; 3], ..st(2, 2.0, t) }] })).unwrap();
        };
        let mut t = 0;
        for _ in 0..8 {
            kf_at(&mut r, t);
            for at in t..t + c.keyframe_ticks {
                assert!(!r.coasting(at), "tick {at}");
                assert!(r.gc(at).is_empty(), "tick {at}");
                assert!(r.extrapolate(at).iter().all(|e| !e.stale && !e.coasting && e.ce == 0.15), "tick {at}");
            }
            t += c.keyframe_ticks;
        }
        // Link dies after the keyframe at `t`.
        kf_at(&mut r, t);
        assert!(!r.coasting(t + c.coast_ticks - 1) && r.coasting(t + c.coast_ticks));
        assert!(!r.extrapolate(t + c.stale_ticks - 1)[0].stale && r.extrapolate(t + c.stale_ticks)[0].stale);
        assert!(r.gc(t + c.drop_ticks - 1).is_empty());
        assert_eq!(r.gc(t + c.drop_ticks), vec![1, 2]);
    }

    #[test]
    fn raising_the_budget_keeps_the_old_limits_for_one_old_coast_period() {
        let slow = cadence(600);
        let mut r = Receiver::new(ReceiverConfig::default());
        r.make_ack(600);
        r.on_datagram(&kf(1, 1000, 0, 1, vec![st(1, 0.0, 1000)])).unwrap();
        // 12 s of normal silence at 600 bit/s, then the operator lifts the budget.
        r.on_datagram(&encode(&Message::Hello { device_id: 1, session_nonce: 1, caps: 0, tick: 1000 + 12 * TICK_HZ })).unwrap();
        r.make_ack(0);
        assert_eq!(r.cadence(), cadence(0));
        let at = 1000 + 13 * TICK_HZ; // entity 13 s old, device silent 1 s
        assert!(!r.extrapolate(at)[0].stale && r.gc(at).is_empty(), "the edge has not heard of the new budget yet");
        // After the grace period the unlimited limits apply.
        let after = 1000 + 12 * TICK_HZ + slow.coast_ticks + 1;
        assert!(r.coasting(after) && r.extrapolate(after)[0].stale);
        // Lowering the budget loosens the limits at once.
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&delta(1, 0, vec![Update::Spawn(st(1, 0.0, 0))])).unwrap();
        r.make_ack(600);
        assert!(!r.coasting(slow.coast_ticks - 1) && !r.extrapolate(slow.stale_ticks - 1)[0].stale);
    }
}
