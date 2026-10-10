//! The receiver: frames in, a world and derived events out (PROTOCOL.md §5). Merge is
//! last-writer-wins per contact by (rev, tick); events come from what the merge changed, stamped
//! with the times inside the records; liveness and the error radius grow with silence.

use crate::classes::{max_speed, motion_thresholds};
use crate::geo::sincos_deg;
use crate::scheduler::{expected_gap, timing, Timing};
use crate::wire::*;
use crate::TICK_HZ;
use serde::Serialize;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Serialize)]
pub struct Held {
    pub rec: ContactRec,
    /// Frame tick of the record held.
    pub frame_tick: u32,
    /// When this revision was first heard (edge ticks), for the expected repeat gap.
    pub rev_heard: u32,
    pub first_heard: u32,
    pub copies: u32,
    /// The revision this receiver last acknowledged in a Digest: the edge may then repeat it only
    /// at `T_floor`, so silence up to the floor is not overdue.
    pub acked_rev: Option<u8>,
}
impl Held {
    pub fn observed(&self) -> u32 { self.frame_tick.saturating_sub(self.rec.age as u32 * TICK_HZ) }
}

#[derive(Clone, Debug, Serialize)]
pub struct Event {
    pub kind: &'static str,
    pub id: u16,
    /// Event time in edge ticks (from the record, not from arrival).
    pub tick: u32,
    pub heard: u32,
    pub text: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct RxContact {
    pub id: u16, pub rev: u8, pub e: f32, pub n: f32, pub ce: f32, pub ce_shown: f32, pub radius: f32, pub count: u32, pub mix: [u8; 4],
    pub motion: &'static str, pub confirmed: bool, pub lost: bool, pub out_of_view: bool, pub departed: bool, pub focused: bool, pub group: bool,
    pub course: f32, pub speed: f32, pub first_seen: f32, pub since: f32, pub age_s: f32, pub silence_s: f32, pub liveness: &'static str,
    pub parent: Option<u16>, pub child: bool, pub ray: Option<[f32; 2]>, pub bbox: Option<[f32; 4]>, pub copies: u32, pub conf: u8,
    pub lat: Option<f64>, pub lon: Option<f64>,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct RxStats { pub frames: u64, pub bytes: u64, pub records: u64, pub rejected: u64, pub stale_copies: u64, pub bad: u64, pub last_seq: u16, pub gaps: u64 }

pub struct Receiver {
    pub session: Option<SessionRec>,
    pub ego: Option<(EgoRec, u32)>,
    pub poses: Vec<PoseRec>,
    pub contacts: BTreeMap<u16, Held>,
    pub events: Vec<Event>,
    pub stats: RxStats,
    pub budget_bps: u32,
    timing: Timing,
    last_tick: u32,
    last_frame_local: Option<u32>,
    last_nav: Option<u8>,
    changed_order: Vec<u16>,
    ack_pending: bool,
}

impl Receiver {
    pub fn new(budget_bps: u32) -> Self {
        Receiver { session: None, ego: None, poses: Vec::new(), contacts: BTreeMap::new(), events: Vec::new(), stats: RxStats::default(), budget_bps,
            timing: timing(budget_bps), last_tick: 0, last_frame_local: None, last_nav: None, changed_order: Vec::new(), ack_pending: false }
    }

    pub fn set_budget(&mut self, bps: u32) { self.budget_bps = bps; self.timing = timing(bps); }
    pub fn last_tick(&self) -> u32 { self.last_tick }
    pub fn timing(&self) -> &Timing { &self.timing }

    /// Apply a downlink frame. Returns the number of records applied (records that were older
    /// than what is held count as applied too: they are expected repeats).
    pub fn on_frame(&mut self, bytes: &[u8]) -> Result<usize, CodecError> {
        let f = match Frame::decode(bytes) { Ok(f) => f, Err(e) => { self.stats.bad += 1; return Err(e); } };
        if f.uplink { return Ok(0); }
        self.stats.frames += 1; self.stats.bytes += bytes.len() as u64;
        if self.stats.frames > 1 && f.seq.wrapping_sub(self.stats.last_seq) > 1 && f.seq.wrapping_sub(self.stats.last_seq) < 0x8000 { self.stats.gaps += (f.seq.wrapping_sub(self.stats.last_seq) - 1) as u64; }
        self.stats.last_seq = f.seq;
        let tick = f.tick;
        if tick > self.last_tick { self.last_tick = tick; }
        self.last_frame_local = Some(tick);
        let mut n = 0;
        for r in f.records {
            n += 1; self.stats.records += 1;
            match r {
                Record::Session(s) => {
                    let new = self.session.map_or(true, |o| o.nonce != s.nonce);
                    if new && self.session.is_some() { self.contacts.clear(); self.poses.clear(); self.ego = None; self.push("session", 0, tick, tick, "new session: origin and ids reset".into()); }
                    self.session = Some(s);
                }
                Record::Ego(e) => {
                    if self.last_nav.map_or(true, |o| o != e.nav) {
                        self.push("ego", 0, tick, tick, format!("drone {} · gnss {} · uplink {}{}", nav_name(e.nav), gnss_name(e.nav), link_name(e.nav), if e.nav & 0x80 != 0 { " · video up" } else { " · video down" }));
                    }
                    self.last_nav = Some(e.nav);
                    self.ego = Some((e, tick));
                }
                Record::Pose(p) => {
                    if let Some(i) = self.poses.iter().position(|q| q.tick == p.tick) { self.poses[i] = p; } else { self.poses.push(p); }
                    if self.poses.len() > 600 { let k = self.poses.len() - 600; self.poses.drain(..k); }
                }
                Record::Contact(c) => self.merge(c, tick),
                _ => {}
            }
        }
        self.ack_pending = true;
        Ok(n)
    }

    fn merge(&mut self, c: ContactRec, tick: u32) {
        let observed = tick.saturating_sub(c.age as u32 * TICK_HZ);
        let text_of = |c: &ContactRec| describe_mix(c);
        match self.contacts.get_mut(&c.id) {
            None => {
                if c.has(F_DEPARTED) {
                    self.contacts.insert(c.id, Held { rec: c, frame_tick: tick, rev_heard: tick, first_heard: tick, copies: 1, acked_rev: None });
                    self.push("departed", c.id, observed, tick, format!("contact {} departed (never seen live here)", c.id));
                    return;
                }
                self.contacts.insert(c.id, Held { rec: c, frame_tick: tick, rev_heard: tick, first_heard: tick, copies: 1, acked_rev: None });
                let fs = c.first_seen as u32 * TICK_HZ;
                self.push("new", c.id, fs, tick, format!("contact {}: {} first seen {}", c.id, text_of(&c), if c.ext_has(X_CHILD) { format!("(member of {})", c.parent) } else { String::new() }));
                if c.has(F_CONFIRMED) && c.motion() == MOTION_MOVING { self.push("moving", c.id, c.since as u32 * TICK_HZ, tick, format!("contact {}: moving, course {:03.0} at {:.1} m/s", c.id, u8_to_deg(c.course), u8_to_speed(c.speed))); }
                if c.has(F_LOST) { self.push("lost", c.id, observed, tick, format!("contact {}: {}", c.id, if c.ext_has(X_OUT_OF_VIEW) { "left the view" } else { "lost by the edge" })); }
                self.changed_order.insert(0, c.id);
            }
            Some(h) => {
                let old = h.rec;
                let newer_rev = (c.rev.wrapping_sub(old.rev) as i8) > 0;
                let same_rev_fresher = c.rev == old.rev && tick >= h.frame_tick;
                let tomb = c.has(F_DEPARTED) && !old.has(F_DEPARTED);
                if old.has(F_DEPARTED) && !c.has(F_DEPARTED) { self.stats.rejected += 1; return; }
                if !(newer_rev || same_rev_fresher || tomb) { self.stats.rejected += 1; return; }
                if c.rev == old.rev && !tomb { self.stats.stale_copies += 1; }
                h.rec = c; h.frame_tick = tick; h.copies += 1;
                if newer_rev || tomb { h.rev_heard = tick; self.changed_order.retain(|&i| i != c.id); self.changed_order.insert(0, c.id); }
                // Derived events from what changed.
                let id = c.id;
                if tomb { self.push("departed", id, observed, tick, format!("contact {}: departed", id)); return; }
                if !old.has(F_CONFIRMED) && c.has(F_CONFIRMED) { self.push("confirmed", id, observed, tick, format!("contact {}: confirmed, {}", id, text_of(&c))); }
                if old.motion() != c.motion() {
                    let since = c.since as u32 * TICK_HZ;
                    match c.motion() {
                        MOTION_MOVING => self.push("moving", id, since, tick, format!("contact {}: {} started moving, course {:03.0} at {:.1} m/s", id, text_of(&c), u8_to_deg(c.course), u8_to_speed(c.speed))),
                        MOTION_STOPPED => self.push("stopped", id, since, tick, format!("contact {}: stopped", id)),
                        MOTION_STATIC => self.push("static", id, since, tick, format!("contact {}: static", id)),
                        _ => {}
                    }
                }
                if old.count() != c.count() {
                    let kind = if c.count() > old.count() { "grew" } else { "shrank" };
                    self.push(kind, id, observed, tick, format!("contact {}: now {} (was {})", id, text_of(&c), old.count()));
                }
                if !old.has(F_LOST) && c.has(F_LOST) { self.push("lost", id, observed, tick, format!("contact {}: {}", id, if c.ext_has(X_OUT_OF_VIEW) { "left the view" } else { "lost by the edge" })); }
                if old.has(F_LOST) && !c.has(F_LOST) { self.push("reacquired", id, observed, tick, format!("contact {}: seen again", id)); }
                if c.ext_has(X_CHILD) && old.parent != c.parent { self.push("split", id, observed, tick, format!("contact {} is now a member of {}", id, c.parent)); }
                if !old.has(F_FOCUSED) && c.has(F_FOCUSED) { self.push("focus", id, observed, tick, format!("contact {}: edge focused", id)); }
            }
        }
    }

    fn push(&mut self, kind: &'static str, id: u16, tick: u32, heard: u32, text: String) {
        self.events.push(Event { kind, id, tick, heard, text });
        if self.events.len() > 1000 { let k = self.events.len() - 1000; self.events.drain(..k); }
    }

    pub fn drain_events(&mut self) -> Vec<Event> { std::mem::take(&mut self.events) }

    /// The world as of edge tick `now`, dead-reckoned, with the honest radius and liveness.
    pub fn snapshot(&self, now: u32) -> Vec<RxContact> {
        let t = &self.timing;
        let pos_res = self.session.map_or(2, |s| s.pos_res);
        let ladder_last = t.ladder[t.ladder.len() - 1];
        self.contacts.values().map(|h| {
            let c = &h.rec;
            let observed = h.observed();
            let silence = now.saturating_sub(observed) as f32 / TICK_HZ as f32;
            let (mut e, mut n) = (pos_to_m(c.dx, pos_res), pos_to_m(c.dy, pos_res));
            let moving = c.motion() == MOTION_MOVING && c.has(F_VELOCITY);
            let coarse = dominant_coarse(c);
            let cap = max_speed(coarse);
            let speed = u8_to_speed(c.speed).min(cap);
            let course = u8_to_deg(c.course);
            if moving && !c.has(F_LOST) && !c.has(F_DEPARTED) {
                let (s, co) = sincos_deg(course);
                e += s * speed * silence; n += co * speed * silence;
            }
            let ce = m8_to_m(c.ce);
            let horizon = if h.acked_rev == Some(c.rev) { t.floor } else { ladder_last };
            let overdue = (silence - horizon as f32 / TICK_HZ as f32).max(0.0);
            let out_of_view = c.has(F_LOST) && c.ext_has(X_OUT_OF_VIEW);
            let (hi, lo) = motion_thresholds(coarse);
            let mut ce_shown = ce;
            // Out of view: the record says where it was last seen, not where it is, so the circle
            // stays the one of that sighting instead of growing over the whole map.
            if !c.has(F_DEPARTED) && !out_of_view {
                match c.motion() {
                    // Moving: the ghost follows the course; the circle grows at the contact's own
                    // speed (covers a stop), at the class cap once overdue on the ladder.
                    MOTION_MOVING if moving => { ce_shown += speed * silence + cap * overdue; }
                    // Static: below `lo` by definition, and the edge revises past `ce`; the creep
                    // the receiver can be wrong by is bounded by one more `ce`.
                    MOTION_STATIC => { ce_shown += (lo * silence).min(ce); }
                    // Stopped, unknown, or moving without a velocity: it may be walking off since
                    // the last look, at `hi` or more; at the class cap once overdue.
                    _ => { ce_shown += hi * silence + cap * overdue; }
                }
            }
            let ce_shown = ce_shown.min(1000.0);
            let since_rev = now.saturating_sub(h.rev_heard);
            let gap = if c.has(F_FOCUSED) { t.focus } else { expected_gap(since_rev, t) };
            let heard_ago = now.saturating_sub(h.frame_tick);
            let liveness = if c.has(F_DEPARTED) { "departed" } else if out_of_view { "out of view" } else if c.has(F_LOST) { "lost" } else if heard_ago > 3 * gap { "unheard" } else { "fresh" };
            let (lat, lon) = match self.session {
                Some(s) => { let (la, lo) = crate::geo::enu_to_latlon(s.origin_lat as f64 * 1e-7, s.origin_lon as f64 * 1e-7, e as f64, n as f64); (Some(la), Some(lo)) }
                None => (None, None),
            };
            RxContact { id: c.id, rev: c.rev, e, n, ce, ce_shown, radius: m8_to_m(c.radius), count: c.count(), mix: [c.n_dismount, c.n_vehicle, c.n_armour, c.n_other],
                motion: motion_name(c.flags), confirmed: c.has(F_CONFIRMED), lost: c.has(F_LOST), out_of_view, departed: c.has(F_DEPARTED), focused: c.has(F_FOCUSED), group: c.has(F_GROUP),
                course, speed: u8_to_speed(c.speed), first_seen: c.first_seen as f32, since: c.since as f32, age_s: c.age as f32, silence_s: silence, liveness,
                parent: if c.ext_has(X_PARENT) { Some(c.parent) } else { None }, child: c.ext_has(X_CHILD),
                ray: if c.ext_has(X_RAY) { Some([u8_to_deg(c.az), u8_to_el(c.el)]) } else { None },
                bbox: if c.ext_has(X_BBOX) { Some([u8_to_nrm(c.bbox[0]), u8_to_nrm(c.bbox[1]), u8_to_nrm(c.bbox[2]), u8_to_nrm(c.bbox[3])]) } else { None },
                copies: h.copies, conf: c.conf, lat, lon }
        }).collect()
    }

    /// Live contacts held vs the edge's own count (`Ego.n_contacts`).
    pub fn known_of(&self) -> (u32, Option<u32>) {
        let known = self.contacts.values().filter(|h| !h.rec.has(F_DEPARTED) && !h.rec.has(F_LOST) && !h.rec.ext_has(X_CHILD)).count() as u32;
        (known, self.ego.map(|(e, _)| e.n_contacts as u32))
    }

    /// No frame for 3 x T_ego of the advertised budget.
    pub fn device_unheard(&self, now: u32) -> bool {
        self.last_frame_local.map_or(true, |t| now.saturating_sub(t) > 3 * self.timing.ego)
    }

    pub fn needs_digest(&self) -> bool { self.ack_pending }

    /// An uplink frame with a `Digest` (up to 32 most recently changed contacts) at `budget_bps`.
    pub fn make_digest(&mut self, budget_bps: u32, seq: u16, now: u32) -> Vec<u8> {
        self.set_budget(budget_bps);
        self.ack_pending = false;
        let acked: Vec<(u16, u8)> = self.changed_order.iter().filter_map(|id| self.contacts.get(id).map(|h| (*id, h.rec.rev))).take(32).collect();
        for (id, rev) in &acked { if let Some(h) = self.contacts.get_mut(id) { h.acked_rev = Some(*rev); } }
        let f = Frame { session: self.session.map_or(0, |s| s.nonce as u16), seq, tick: now, uplink: true, cycle_end: false,
            records: vec![Record::Digest(DigestRec { last_seq: self.stats.last_seq, budget_10bps: (budget_bps / 10).min(65535) as u16, acked })] };
        f.encode(false)
    }

    pub fn make_focus(&self, id: u16, mode: u8, ttl: u8, chip_px: u8, seq: u16, now: u32) -> Vec<u8> {
        let f = Frame { session: self.session.map_or(0, |s| s.nonce as u16), seq, tick: now, uplink: true, cycle_end: false,
            records: vec![Record::Focus(FocusRec { id, mode, ttl, chip_px })] };
        f.encode(false)
    }

    /// Several focus commands in one uplink frame, applied in order. Drilling into a split group is
    /// `[(child, track), (group, release)]`: one frame, so the pick and the release arrive together.
    pub fn make_focus_many(&self, cmds: &[(u16, u8)], ttl: u8, seq: u16, now: u32) -> Vec<u8> {
        let f = Frame { session: self.session.map_or(0, |s| s.nonce as u16), seq, tick: now, uplink: true, cycle_end: false,
            records: cmds.iter().map(|&(id, mode)| Record::Focus(FocusRec { id, mode, ttl, chip_px: 0 })).collect() };
        f.encode(false)
    }

    /// Forget tombstones older than 2 x floor (they did their job).
    pub fn gc(&mut self, now: u32) {
        let life = 2 * self.timing.floor;
        self.contacts.retain(|_, h| !(h.rec.has(F_DEPARTED) && now.saturating_sub(h.frame_tick) > life));
    }
}

fn dominant_coarse(c: &ContactRec) -> u8 {
    let mix = [c.n_dismount, c.n_vehicle, c.n_armour, c.n_other];
    let mut best = 3u8; let mut bn = 0u8;
    for i in (0..4u8).rev() { if mix[i as usize] >= bn && mix[i as usize] > 0 { best = i; bn = mix[i as usize]; } }
    best
}

pub fn describe_mix(c: &ContactRec) -> String {
    let mut parts = Vec::new();
    if c.n_dismount > 0 { parts.push(format!("{} dismount{}", c.n_dismount, if c.n_dismount > 1 { "s" } else { "" })); }
    if c.n_vehicle > 0 { parts.push(format!("{} vehicle{}", c.n_vehicle, if c.n_vehicle > 1 { "s" } else { "" })); }
    if c.n_armour > 0 { parts.push(format!("{} armoured", c.n_armour)); }
    if c.n_other > 0 { parts.push(format!("{} mover{}", c.n_other, if c.n_other > 1 { "s" } else { "" })); }
    if parts.is_empty() { "nothing".into() } else { parts.join(", ") }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contact(id: u16, rev: u8, flags: u8, dx: i16, dy: i16, age: u8) -> ContactRec {
        ContactRec { id, rev, flags, dx, dy, age, n_vehicle: 2, ce: m_to_m8(6.0), first_seen: 10, since: 12, course: deg_to_u8(90.0), speed: speed_to_u8(4.0), ..Default::default() }
    }
    fn frame(seq: u16, tick: u32, recs: Vec<Record>) -> Vec<u8> { Frame { session: 1, seq, tick, uplink: false, cycle_end: false, records: recs }.encode(false) }

    /// A vehicle that drove out of the frame keeps the circle of its last sighting; one lost in
    /// view still grows. And a record this receiver acked is not overdue before T_floor.
    #[test]
    fn out_of_view_does_not_inflate_and_acks_extend_the_horizon() {
        let mut rx = Receiver::new(9600);
        let gone = ContactRec { ext: X_OUT_OF_VIEW, ..contact(1, 2, MOTION_STOPPED | F_CONFIRMED | F_LOST, 0, 0, 5) };
        let hidden = contact(2, 2, MOTION_STOPPED | F_CONFIRMED | F_LOST, 50, 0, 5);
        rx.on_frame(&frame(1, 600, vec![Record::Contact(gone), Record::Contact(hidden)])).unwrap();
        let s = rx.snapshot(60 * TICK_HZ);
        let (a, b) = (s.iter().find(|c| c.id == 1).unwrap(), s.iter().find(|c| c.id == 2).unwrap());
        assert_eq!(a.liveness, "out of view"); assert!(a.out_of_view);
        assert!((a.ce_shown - 6.0).abs() < 0.01, "last sighting's circle: {}", a.ce_shown);
        assert!(b.ce_shown > 50.0, "lost in view still grows: {}", b.ce_shown);
        assert_eq!(rx.events.iter().filter(|e| e.kind == "lost").count(), 2);
        assert!(rx.events.iter().any(|e| e.text.contains("left the view")));

        // Stopped car, heard at 0 s; 9.6 kbit/s: ladder ends at 2.5 s, floor 10 s.
        let mut rx = Receiver::new(9600);
        rx.on_frame(&frame(1, 0, vec![Record::Contact(contact(3, 1, MOTION_STOPPED | F_CONFIRMED, 0, 0, 0))])).unwrap();
        let before = rx.snapshot(8 * TICK_HZ)[0].ce_shown;
        rx.make_digest(9600, 0, 1);
        let after = rx.snapshot(8 * TICK_HZ)[0].ce_shown;
        assert!(before > 100.0, "unacked, 5.5 s overdue at the vehicle cap: {before}");
        assert!(after < 15.0, "acked: within the floor, only the stopped creep: {after}");
    }

    #[test]
    fn merge_is_lww_and_order_free() {
        let mut rx = Receiver::new(800);
        let a = contact(1, 1, MOTION_STATIC | F_CONFIRMED, 100, 0, 0);
        let b = contact(1, 2, MOTION_MOVING | F_CONFIRMED | F_VELOCITY, 110, 0, 0);
        rx.on_frame(&frame(2, 2400, vec![Record::Contact(b)])).unwrap();
        rx.on_frame(&frame(1, 1200, vec![Record::Contact(a)])).unwrap();
        assert_eq!(rx.contacts[&1].rec.rev, 2, "the older revision never regresses newer state");
        assert_eq!(rx.stats.rejected, 1);
        // A repeat of rev 2 with a newer tick refreshes the age.
        rx.on_frame(&frame(3, 3600, vec![Record::Contact(b)])).unwrap();
        assert_eq!(rx.contacts[&1].frame_tick, 3600); assert_eq!(rx.contacts[&1].copies, 2);
        let kinds: Vec<&str> = rx.events.iter().map(|e| e.kind).collect();
        assert_eq!(kinds, vec!["new", "moving"]);
        assert_eq!(rx.events[0].tick, 10 * TICK_HZ, "new is stamped with first_seen, not arrival");
        // Tombstone wins and sticks.
        let t = contact(1, 3, MOTION_STATIC | F_DEPARTED, 110, 0, 0);
        rx.on_frame(&frame(4, 4800, vec![Record::Contact(t)])).unwrap();
        rx.on_frame(&frame(5, 6000, vec![Record::Contact(contact(1, 4, MOTION_STATIC, 0, 0, 0))])).unwrap();
        assert!(rx.contacts[&1].rec.has(F_DEPARTED));
        assert_eq!(rx.events.last().unwrap().kind, "departed");
    }

    #[test]
    fn dead_reckoning_and_the_growing_radius() {
        let mut rx = Receiver::new(800);
        let b = contact(1, 1, MOTION_MOVING | F_CONFIRMED | F_VELOCITY, 0, 0, 0);
        rx.on_frame(&frame(1, 0, vec![Record::Contact(b)])).unwrap();
        let s = rx.snapshot(10 * TICK_HZ);
        assert!((s[0].e - 40.0).abs() < 0.1, "4 m/s east for 10 s: {}", s[0].e);
        assert!((s[0].ce_shown - (6.0 + 40.0)).abs() < 0.6, "{}", s[0].ce_shown);
        assert_eq!(s[0].liveness, "fresh");
        let s = rx.snapshot(400 * TICK_HZ);
        assert_eq!(s[0].liveness, "unheard");
        assert!(s[0].ce_shown >= 1000.0 - 1.0 || s[0].ce_shown > 400.0);
        // A static contact's circle creeps at the class's `lo` threshold (0.3 m/s for a vehicle)
        // and stops at twice its ce; it stays fresh for 3 floors.
        let st = contact(2, 1, MOTION_STATIC | F_CONFIRMED, 50, 50, 0);
        rx.on_frame(&frame(2, 0, vec![Record::Contact(st)])).unwrap();
        let s = rx.snapshot(10 * TICK_HZ);
        let c2 = s.iter().find(|c| c.id == 2).unwrap();
        assert!((c2.ce_shown - 9.0).abs() < 0.01, "6 + 0.3 x 10: {}", c2.ce_shown);
        let s = rx.snapshot(100 * TICK_HZ);
        let c2 = s.iter().find(|c| c.id == 2).unwrap();
        assert_eq!(c2.ce_shown, 12.0); assert_eq!(c2.liveness, "fresh");
        // A stopped dismount may be walking off: `hi` (0.5 m/s) from the first second, the 3 m/s
        // cap once overdue on the ladder (30 s at f = 1); the same for an unknown motion state.
        let mut sp = contact(3, 1, MOTION_STOPPED | F_CONFIRMED, 0, 0, 0); sp.n_vehicle = 0; sp.n_dismount = 1;
        rx.on_frame(&frame(3, 0, vec![Record::Contact(sp)])).unwrap();
        let at = |rx: &Receiver, t: u32| rx.snapshot(t * TICK_HZ).iter().find(|c| c.id == 3).unwrap().ce_shown;
        assert!((at(&rx, 10) - 11.0).abs() < 0.01, "{}", at(&rx, 10));
        assert!((at(&rx, 40) - (6.0 + 20.0 + 30.0)).abs() < 0.01, "{}", at(&rx, 40));
    }

    #[test]
    fn digest_lists_recent_revisions_and_session_gives_latlon() {
        let mut rx = Receiver::new(800);
        let s = SessionRec { nonce: 7, device_id: 1, origin_lat: 393500000, origin_lon: -857000000, origin_alt: 0x7FFF, pos_res: 2, caps: 0, utc_at_tick0: 0, hfov_x10: 850, img_w: 10, img_h: 10, video_frame0: 0, fps_x100: 0 };
        rx.on_frame(&frame(1, 0, vec![Record::Session(s), Record::Contact(contact(1, 1, MOTION_STATIC, 1000, 0, 0)), Record::Contact(contact(2, 1, MOTION_STATIC, 0, 1000, 0))])).unwrap();
        let snap = rx.snapshot(0);
        assert!((snap[0].lon.unwrap() + 85.7 - 0.01163).abs() < 2e-4);
        assert!((snap[1].lat.unwrap() - 39.35 - 0.00899).abs() < 1e-4);
        let d = rx.make_digest(800, 1, 0);
        let f = Frame::decode(&d).unwrap();
        assert!(f.uplink);
        match &f.records[0] { Record::Digest(dg) => { assert_eq!(dg.acked, vec![(2, 1), (1, 1)]); assert_eq!(dg.budget_10bps, 80); assert_eq!(dg.last_seq, 1); } _ => panic!() }
        assert_eq!(rx.known_of(), (2, None));
    }
}
