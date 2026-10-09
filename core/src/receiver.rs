//! Twin-side sync for one device: apply datagrams, detect gaps, produce acks, extrapolate.

use crate::predictor::Predictor;
use crate::wire::{decode, encode, CodecError, EntityState, Message, Update};
use crate::TICK_HZ;

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

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Extrapolated {
    pub state: EntityState,
    /// Ticks since the entity was last refreshed by the edge.
    pub age_ticks: u32,
    pub stale: bool,
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

pub struct Receiver {
    cfg: ReceiverConfig,
    entities: Vec<EntityState>,
    /// Last state refresh per entity: (id, tick). Kept separate from `EntityState.tick` which is
    /// the observation tick and advances under extrapolation.
    last_seq: Option<u32>,
    /// Open gaps: (seq, edge tick when noticed, edge tick of the last nack).
    gaps: Vec<(u32, u32, Option<u32>)>,
    /// Keyframe parts seen for the keyframe at `tick`: (tick, of, part mask, ids).
    keyframe: Option<(u32, u8, u64, Vec<u32>)>,
    last_edge_tick: u32,
    device_id: Option<u32>,
    session_nonce: Option<u32>,
    pose: Option<Pose>,
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
            device_id: None,
            session_nonce: None,
            pose: None,
            stats: ReceiverStats::default(),
        }
    }

    pub fn stats(&self) -> ReceiverStats { self.stats }
    pub fn device_id(&self) -> Option<u32> { self.device_id }
    pub fn pose(&self) -> Option<Pose> { self.pose }
    /// Latest edge tick seen in any message; the host maps this to its own clock.
    pub fn last_edge_tick(&self) -> u32 { self.last_edge_tick }
    pub fn needs_ack(&self) -> bool {
        let now = self.last_edge_tick;
        self.gaps.iter().any(|(_, since, last)| self.nack_due(now, *since, *last))
    }

    fn nack_due(&self, now: u32, since: u32, last: Option<u32>) -> bool {
        now.wrapping_sub(since) >= self.cfg.gap_nack_ticks
            && match last { None => true, Some(t) => now.wrapping_sub(t) >= self.cfg.renack_ticks }
    }

    pub fn on_datagram(&mut self, bytes: &[u8]) -> Result<Event, CodecError> {
        let msg = decode(bytes)?;
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
                }
                self.session_nonce = Some(session_nonce);
                self.device_id = Some(device_id);
                self.note_tick(tick);
                Event::Hello { device_id, session_nonce }
            }
            Message::Delta { seq, tick, updates } => {
                self.note_tick(tick);
                if !self.note_seq(seq) {
                    return Ok(Event::Ignored);
                }
                self.stats.deltas += 1;
                let mut applied = 0;
                for u in updates {
                    if self.apply(u) {
                        applied += 1;
                    }
                }
                Event::Delta { seq, applied }
            }
            Message::Keyframe { seq, tick, part, of, entities } => {
                self.note_tick(tick);
                if !self.note_seq(seq) {
                    return Ok(Event::Ignored);
                }
                self.stats.keyframes += 1;
                let n = entities.len();
                let ids: Vec<u32> = entities.iter().map(|e| e.id).collect();
                for e in entities {
                    self.apply(Update::Update(e));
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

    /// Build an ack datagram. Call every ~100 ms or when `needs_ack()`.
    pub fn make_ack(&mut self, budget_bps: u32) -> Vec<u8> {
        let now = self.last_edge_tick;
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
        self.entities
            .iter()
            .map(|e| {
                let age = at_tick.wrapping_sub(e.tick);
                Extrapolated { state: Predictor::step(e, at_tick), age_ticks: age, stale: age >= self.cfg.stale_ticks }
            })
            .collect()
    }

    /// Drop stale entities. While the device is alive, removal is driven by Despawns and
    /// keyframe reconciliation; age-based dropping applies when the device has gone silent
    /// (`stale_ticks`) or unconditionally after `hard_drop_ticks`. Returns dropped ids.
    pub fn gc(&mut self, at_tick: u32) -> Vec<u32> {
        let drop = self.cfg.drop_ticks;
        let hard = self.cfg.hard_drop_ticks;
        let device_silent = at_tick.wrapping_sub(self.last_edge_tick) >= self.cfg.stale_ticks;
        let mut dropped = Vec::new();
        self.entities.retain(|e| {
            let age = at_tick.wrapping_sub(e.tick);
            if (device_silent && age >= drop) || age >= hard {
                dropped.push(e.id);
                false
            } else {
                true
            }
        });
        dropped
    }

    pub fn raw_entities(&self) -> &[EntityState] { &self.entities }

    /// Once every part of a keyframe has arrived, entities it does not list (and that were not
    /// observed after it) are gone: this repairs lost Despawns without waiting for gc.
    fn reconcile_keyframe(&mut self, tick: u32, part: u8, of: u8, ids: Vec<u32>) {
        if of == 0 || of > 64 {
            return;
        }
        let (mask, mut all_ids) = match self.keyframe.take() {
            Some((t, o, m, v)) if t == tick && o == of => (m, v),
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
        self.entities.retain(|e| all_ids.contains(&e.id) || tick.wrapping_sub(e.tick) >= u32::MAX / 2);
        self.stats.reconciled += (before - self.entities.len()) as u32;
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
    fn apply(&mut self, u: Update) -> bool {
        match u {
            Update::Spawn(s) | Update::Update(s) => {
                match self.entities.iter_mut().find(|e| e.id == s.id) {
                    Some(e) => {
                        if s.tick.wrapping_sub(e.tick) < u32::MAX / 2 {
                            *e = s;
                            true
                        } else {
                            false
                        }
                    }
                    None => {
                        self.entities.push(s);
                        true
                    }
                }
            }
            Update::Despawn { id, tick } => {
                let before = self.entities.len();
                self.entities.retain(|e| !(e.id == id && tick.wrapping_sub(e.tick) < u32::MAX / 2));
                self.entities.len() != before
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::classes::PERSON;

    fn st(id: u32, x: f32, tick: u32) -> EntityState {
        EntityState { id, class: PERSON, pos: [x, 0.0, 0.0], vel: [1.0, 0.0, 0.0], conf: 200, tick }
    }

    #[test]
    fn applies_and_extrapolates() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&encode(&Message::Delta { seq: 1, tick: 0, updates: vec![Update::Spawn(st(1, 0.0, 0))] })).unwrap();
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
        r.on_datagram(&encode(&Message::Delta { seq: 1, tick: 0, updates: vec![] })).unwrap();
        r.on_datagram(&encode(&Message::Delta { seq: 3, tick: 10, updates: vec![] })).unwrap();
        assert_eq!(r.stats().gaps_detected, 1);
        assert!(!r.needs_ack());
        r.on_datagram(&encode(&Message::Delta { seq: 4, tick: 40, updates: vec![] })).unwrap();
        assert!(r.needs_ack());
        let ack = r.make_ack(0);
        match decode(&ack).unwrap() {
            Message::Ack { last_seq: 4, missing, .. } => assert_eq!(missing, vec![2]),
            m => panic!("{m:?}"),
        }
        // Late arrival of seq 2 closes the gap.
        assert_ne!(r.on_datagram(&encode(&Message::Delta { seq: 2, tick: 5, updates: vec![] })).unwrap(), Event::Ignored);
        assert!(!r.needs_ack());
    }

    #[test]
    fn gap_is_renacked_only_after_renack_interval() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&encode(&Message::Delta { seq: 1, tick: 0, updates: vec![] })).unwrap();
        r.on_datagram(&encode(&Message::Delta { seq: 3, tick: 40, updates: vec![] })).unwrap(); // gap: seq 2, since tick 40
        r.on_datagram(&encode(&Message::Delta { seq: 4, tick: 70, updates: vec![] })).unwrap();
        assert!(r.needs_ack());
        r.make_ack(0);
        assert!(!r.needs_ack(), "just nacked");
        r.on_datagram(&encode(&Message::Delta { seq: 5, tick: 80, updates: vec![] })).unwrap();
        match decode(&r.make_ack(0)).unwrap() {
            Message::Ack { missing, .. } => assert!(missing.is_empty(), "no repeat nack within renack_ticks"),
            m => panic!("{m:?}"),
        }
        assert_eq!(r.stats().nacks_sent, 1);
        r.on_datagram(&encode(&Message::Delta { seq: 6, tick: 70 + TICK_HZ, updates: vec![] })).unwrap();
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
        r.on_datagram(&encode(&Message::Delta { seq: 1, tick: 10, updates: vec![Update::Spawn(st(1, 0.0, 10)), Update::Spawn(st(2, 1.0, 10))] })).unwrap();
        // Despawn of 2 at seq 2 is lost. Keyframe in two parts at tick 100 lists only 1 and 3.
        r.on_datagram(&encode(&Message::Keyframe { seq: 3, tick: 100, part: 0, of: 2, entities: vec![st(1, 0.5, 100)] })).unwrap();
        assert_eq!(r.raw_entities().len(), 2, "incomplete keyframe must not remove anything");
        // An entity observed after the keyframe tick survives reconciliation.
        r.on_datagram(&encode(&Message::Delta { seq: 4, tick: 101, updates: vec![Update::Spawn(st(9, 0.0, 101))] })).unwrap();
        r.on_datagram(&encode(&Message::Keyframe { seq: 5, tick: 100, part: 1, of: 2, entities: vec![st(3, 2.0, 100)] })).unwrap();
        let ids: Vec<u32> = r.raw_entities().iter().map(|e| e.id).collect();
        assert_eq!(ids, vec![1, 9, 3]);
        assert_eq!(r.stats().reconciled, 1);
    }

    #[test]
    fn gc_waits_for_device_silence_unless_hard_limit() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&encode(&Message::Delta { seq: 1, tick: 0, updates: vec![Update::Spawn(st(1, 0.0, 0))] })).unwrap();
        // Device keeps talking (poses) but the entity is never refreshed.
        r.on_datagram(&encode(&Message::Pose { seq: 2, tick: 11 * TICK_HZ, pos: [0.0; 3], quat: [0.0, 0.0, 0.0, 1.0], origin_locked: true })).unwrap();
        assert!(r.gc(11 * TICK_HZ).is_empty(), "device alive: keep until keyframe reconciliation");
        assert_eq!(r.gc(31 * TICK_HZ), vec![1], "hard limit");
    }

    #[test]
    fn older_observation_never_overwrites_newer() {
        let mut r = Receiver::new(ReceiverConfig::default());
        r.on_datagram(&encode(&Message::Delta { seq: 1, tick: 100, updates: vec![Update::Spawn(st(1, 5.0, 100))] })).unwrap();
        // A keyframe that was delayed (lower seq, older tick) must not move the entity back.
        r.on_datagram(&encode(&Message::Delta { seq: 3, tick: 101, updates: vec![] })).unwrap();
        r.on_datagram(&encode(&Message::Keyframe { seq: 2, tick: 50, part: 0, of: 1, entities: vec![st(1, 0.0, 50)] })).unwrap();
        assert_eq!(r.raw_entities()[0].pos[0], 5.0);
    }
}
