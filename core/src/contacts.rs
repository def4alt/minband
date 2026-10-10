//! The contact manager: tracks in, contacts out. Per track a motion state machine; per tick a
//! single-linkage grouping with hysteresis; per contact the fields the wire carries, the revision,
//! and change detection against what was last *sent* (DESIGN.md §3.2-3.3, PROTOCOL.md §6.3).

use crate::classes::{coarse, motion_thresholds, COARSE_ARMOUR, COARSE_DISMOUNT, COARSE_VEHICLE};
use crate::geo::course_speed;
use crate::wire::{MOTION_MOVING, MOTION_STATIC, MOTION_STOPPED, MOTION_UNKNOWN};
use crate::TICK_HZ;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// One tracker output per tick, in ENU metres around the session origin. `ce` is the edge's own
/// error radius for this track (the edge computes it from the geometry when the tracker does not).
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Track {
    pub id: u32,
    pub class: u8,
    pub e: f32,
    pub n: f32,
    #[serde(default)]
    pub ve: f32,
    #[serde(default)]
    pub vn: f32,
    #[serde(default = "default_conf")]
    pub conf: u8,
    #[serde(default)]
    pub ce: Option<f32>,
    /// Normalised image box (centre u, v, width, height in 0..1), when the edge has a camera.
    #[serde(default)]
    pub bbox: Option<[f32; 4]>,
}
fn default_conf() -> u8 { 128 }

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ContactConfig {
    /// Minimum link distance for grouping (m); the actual link is max(link_m, 2 ce).
    pub link_m: f32,
    /// Link distance between dismounts (m): people 15 m apart are not a group, vehicles may be.
    pub link_dismount_m: f32,
    /// Hysteresis: members stay linked up to this multiple of the link distance.
    pub stay_factor: f32,
    pub speed_tol: f32,
    pub course_tol_deg: f32,
    pub confirm_looks: u32,
    pub confirm_ticks: u32,
    pub t_moving: u32,
    pub t_stopping: u32,
    pub t_lost: u32,
    pub t_depart: u32,
    pub t_stopped: u32,
    /// Position change threshold as a multiple of `max(ce, 2 pos_res)` (PROTOCOL.md §6.3); 1.0 is the spec.
    pub dev_factor: f32,
    /// A track whose cluster differs from its contact's keeps its contact this long (split and
    /// merge hysteresis), so a flickering crowd does not mint ids.
    pub t_split: u32,
    /// Fallback ce (m) when a track carries none and the edge gives none.
    pub default_ce: f32,
}
impl Default for ContactConfig {
    fn default() -> Self {
        ContactConfig { link_m: 15.0, link_dismount_m: 8.0, stay_factor: 1.5, speed_tol: 1.5, course_tol_deg: 30.0, confirm_looks: 3, confirm_ticks: 2 * TICK_HZ,
            t_moving: 2 * TICK_HZ, t_stopping: 5 * TICK_HZ, t_lost: 5 * TICK_HZ, t_depart: 60 * TICK_HZ, t_stopped: 30 * TICK_HZ, t_split: 3 * TICK_HZ, default_ce: 5.0, dev_factor: 1.0 }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct TrackState {
    pub id: u32,
    pub class: u8,
    pub e: f32, pub n: f32, pub ve: f32, pub vn: f32,
    pub conf: u8,
    pub ce: f32,
    pub bbox: Option<[f32; 4]>,
    pub first_seen: u32,
    pub last_seen: u32,
    pub looks: u32,
    pub motion: u8,
    pub since: u32,
    above_since: Option<u32>,
    below_since: Option<u32>,
    calm_since: Option<u32>,
    outside_since: Option<u32>,
    pub lost: bool,
    pub contact: Option<u16>,
}
impl TrackState {
    pub fn confirmed(&self, cfg: &ContactConfig, now: u32) -> bool {
        self.looks >= cfg.confirm_looks || now.saturating_sub(self.first_seen) >= cfg.confirm_ticks
    }
    fn step_motion(&mut self, cfg: &ContactConfig, now: u32) {
        let (hi, lo) = motion_thresholds(coarse(self.class));
        let speed = (self.ve * self.ve + self.vn * self.vn).sqrt();
        if speed > hi { self.above_since.get_or_insert(now); } else { self.above_since = None; }
        if speed < lo { self.below_since.get_or_insert(now); } else { self.below_since = None; }
        if speed <= hi { self.calm_since.get_or_insert(now); } else { self.calm_since = None; }
        let above = self.above_since.map_or(false, |t| now - t >= cfg.t_moving);
        let below = self.below_since.map_or(false, |t| now - t >= cfg.t_stopping);
        let calm = self.calm_since.map_or(false, |t| now - t >= cfg.t_stopping);
        let next = match self.motion {
            MOTION_MOVING => if below { MOTION_STOPPED } else { MOTION_MOVING },
            MOTION_STOPPED => if above { MOTION_MOVING } else if now - self.since >= cfg.t_stopped { MOTION_STATIC } else { MOTION_STOPPED },
            MOTION_STATIC => if above { MOTION_MOVING } else { MOTION_STATIC },
            _ => if above { MOTION_MOVING } else if calm { MOTION_STATIC } else { MOTION_UNKNOWN },
        };
        if next != self.motion {
            // The state began when the evidence for it started, not when the timer ran out.
            self.since = match next {
                MOTION_MOVING => self.above_since.unwrap_or(now),
                MOTION_STOPPED => self.below_since.unwrap_or(now),
                MOTION_STATIC if self.motion == MOTION_STOPPED => now,
                MOTION_STATIC => self.calm_since.or(self.below_since).unwrap_or(now),
                _ => now,
            };
            self.motion = next;
        }
    }
}

/// What the receiver believes about a contact: the state as of the last record the scheduler
/// emitted. Change detection dead-reckons it to now and compares.
#[derive(Clone, Copy, Debug, Serialize)]
pub struct Sent {
    pub tick: u32,
    pub e: f32, pub n: f32, pub ve: f32, pub vn: f32,
    pub motion: u8, pub confirmed: bool, pub lost: bool, pub departed: bool,
    pub mix: [u8; 4], pub ce: f32, pub course: f32, pub speed: f32,
}

#[derive(Clone, Debug, Serialize)]
pub struct Contact {
    pub id: u16,
    pub rev: u8,
    pub members: Vec<u32>,
    pub e: f32, pub n: f32,
    pub ve: f32, pub vn: f32,
    pub course: f32, pub speed: f32,
    pub radius: f32,
    pub ce: f32,
    pub mix: [u8; 4],
    pub conf: u8,
    pub first_seen: u32,
    pub since: u32,
    pub last_seen: u32,
    pub motion: u8,
    pub confirmed: bool,
    pub lost: bool,
    pub departed: bool,
    pub departed_at: Option<u32>,
    pub focused: bool,
    pub split: bool,
    pub parent: Option<u16>,
    pub bbox: Option<[f32; 4]>,
    pub sent: Option<Sent>,
    /// Set when `rev` moved and the new revision has not been emitted yet.
    pub dirty: bool,
    pub rev_tick: u32,
}
impl Contact {
    fn new(id: u16, now: u32) -> Self {
        Contact { id, rev: 0, members: Vec::new(), e: 0.0, n: 0.0, ve: 0.0, vn: 0.0, course: 0.0, speed: 0.0, radius: 0.0, ce: 0.0, mix: [0; 4], conf: 0,
            first_seen: now, since: now, last_seen: now, motion: MOTION_UNKNOWN, confirmed: false, lost: false, departed: false, departed_at: None,
            focused: false, split: false, parent: None, bbox: None, sent: None, dirty: true, rev_tick: now }
    }
    pub fn count(&self) -> u32 { self.mix.iter().map(|&m| m as u32).sum() }
    pub fn is_group(&self) -> bool { self.count() > 1 }
    pub fn is_child(&self) -> bool { self.parent.is_some() }
    pub fn moving(&self) -> bool { self.motion == MOTION_MOVING }
    /// The coarse class that dominates the mix (ties: the first in dismount, vehicle, armour, other).
    pub fn dominant_coarse(&self) -> u8 {
        let mut best = 3u8; let mut bn = 0u8;
        for c in (0..4u8).rev() { if self.mix[c as usize] >= bn && self.mix[c as usize] > 0 { best = c; bn = self.mix[c as usize]; } }
        best
    }
    /// The receiver's dead reckoning of the last sent state at `now`.
    pub fn ghost(&self, now: u32) -> Option<(f32, f32)> {
        let s = self.sent?;
        let dt = now.saturating_sub(s.tick) as f32 / TICK_HZ as f32;
        if s.motion == MOTION_MOVING { Some((s.e + s.ve * dt, s.n + s.vn * dt)) } else { Some((s.e, s.n)) }
    }
    pub fn snapshot(&self, now: u32) -> Sent {
        Sent { tick: now, e: self.e, n: self.n, ve: self.ve, vn: self.vn, motion: self.motion, confirmed: self.confirmed, lost: self.lost,
            departed: self.departed, mix: self.mix, ce: self.ce, course: self.course, speed: self.speed }
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct ContactStats { pub tracks: u32, pub contacts: u32, pub groups: u32, pub revisions: u64, pub births: u32, pub departures: u32,
    /// Why revisions moved: [first send, state machine or mix, position deviation, ce, course, speed].
    pub rev_why: [u64; 6] }

pub struct ContactManager {
    pub cfg: ContactConfig,
    pub tracks: BTreeMap<u32, TrackState>,
    pub contacts: BTreeMap<u16, Contact>,
    next_id: u16,
    child_ids: BTreeMap<u32, u16>,
    pub stats: ContactStats,
    pos_res_m: f32,
}

impl ContactManager {
    pub fn new(cfg: ContactConfig, pos_res_m: f32) -> Self {
        ContactManager { cfg, tracks: BTreeMap::new(), contacts: BTreeMap::new(), next_id: 1, child_ids: BTreeMap::new(), stats: ContactStats::default(), pos_res_m }
    }

    fn alloc_id(&mut self) -> u16 { let id = self.next_id; self.next_id = self.next_id.wrapping_add(1).max(1); id }

    /// Ingest this tick's tracks; returns the ids of contacts whose revision moved.
    pub fn update(&mut self, tracks: &[Track], now: u32) -> Vec<u16> {
        self.ingest(tracks, now);
        self.age_tracks(now);
        self.group(now);
        self.children(now);
        self.recompute(now);
        self.detect_changes(now)
    }

    fn ingest(&mut self, tracks: &[Track], now: u32) {
        for t in tracks {
            let ce = t.ce.unwrap_or(self.cfg.default_ce);
            let st = self.tracks.entry(t.id).or_insert_with(|| TrackState {
                id: t.id, class: t.class, e: t.e, n: t.n, ve: t.ve, vn: t.vn, conf: t.conf, ce, bbox: t.bbox, first_seen: now, last_seen: now, looks: 0,
                motion: MOTION_UNKNOWN, since: now, above_since: None, below_since: None, calm_since: None, outside_since: None, lost: false, contact: None,
            });
            st.class = t.class; st.e = t.e; st.n = t.n; st.ve = t.ve; st.vn = t.vn; st.conf = t.conf; st.ce = ce; st.bbox = t.bbox;
            st.looks += 1;
            if st.lost { st.lost = false; st.since = now; st.above_since = None; st.below_since = None; st.calm_since = None; }
            st.last_seen = now;
            st.step_motion(&self.cfg, now);
        }
        self.stats.tracks = self.tracks.len() as u32;
    }

    fn age_tracks(&mut self, now: u32) {
        let cfg = self.cfg;
        let mut gone = Vec::new();
        for (id, st) in self.tracks.iter_mut() {
            let silent = now.saturating_sub(st.last_seen);
            if silent >= cfg.t_depart { gone.push(*id); continue; }
            if silent >= cfg.t_lost && !st.lost { st.lost = true; }
        }
        for id in gone {
            if let Some(st) = self.tracks.remove(&id) {
                if let Some(c) = st.contact.and_then(|c| self.contacts.get_mut(&c)) { c.members.retain(|&m| m != id); }
            }
            if let Some(cid) = self.child_ids.remove(&id) { if let Some(c) = self.contacts.get_mut(&cid) { c.members.clear(); } }
        }
    }

    /// Single-linkage clustering of confirmed, non-lost, non-child tracks; stable contact ids.
    fn group(&mut self, now: u32) {
        let cfg = self.cfg;
        let ids: Vec<u32> = self.tracks.values().filter(|t| !t.lost && t.confirmed(&cfg, now)).map(|t| t.id).collect();
        let n = ids.len();
        let mut parent: Vec<usize> = (0..n).collect();
        fn find(p: &mut Vec<usize>, i: usize) -> usize { let mut r = i; while p[r] != r { r = p[r]; } let mut j = i; while p[j] != r { let k = p[j]; p[j] = r; j = k; } r }
        for i in 0..n {
            for j in (i + 1)..n {
                let (a, b) = (&self.tracks[&ids[i]], &self.tracks[&ids[j]]);
                let same = a.contact.is_some() && a.contact == b.contact;
                let base = if coarse(a.class) == COARSE_DISMOUNT && coarse(b.class) == COARSE_DISMOUNT { cfg.link_dismount_m } else { cfg.link_m };
                let link = base.max(2.0 * a.ce.max(b.ce)) * if same { cfg.stay_factor } else { 1.0 };
                let d = ((a.e - b.e).powi(2) + (a.n - b.n).powi(2)).sqrt();
                if d > link { continue; }
                let am = a.motion == MOTION_MOVING; let bm = b.motion == MOTION_MOVING;
                if am != bm { continue; }
                if am {
                    let (ca, sa) = course_speed(a.ve, a.vn); let (cb, sb) = course_speed(b.ve, b.vn);
                    let mut dc = (ca - cb).abs(); if dc > 180.0 { dc = 360.0 - dc; }
                    if (sa - sb).abs() > cfg.speed_tol || dc > cfg.course_tol_deg { continue; }
                }
                let (ri, rj) = (find(&mut parent, i), find(&mut parent, j));
                if ri != rj { parent[ri] = rj; }
            }
        }
        let mut clusters: BTreeMap<usize, Vec<u32>> = BTreeMap::new();
        for i in 0..n { let r = find(&mut parent, i); clusters.entry(r).or_default().push(ids[i]); }
        let mut clusters: Vec<Vec<u32>> = clusters.into_values().collect();
        clusters.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a[0].cmp(&b[0])));

        // Claim: each cluster takes the contact most of its members already belong to; a contact
        // can be claimed once; ties go to the oldest contact. A track whose cluster is not its
        // contact's keeps its contact for `t_split` (patience on both splits and merges).
        let mut claimed: BTreeMap<u16, Vec<u32>> = BTreeMap::new();
        let mut unclaimed: Vec<Vec<u32>> = Vec::new();
        for cl in clusters {
            let mut votes: BTreeMap<u16, usize> = BTreeMap::new();
            for id in &cl { if let Some(c) = self.tracks[id].contact { if self.contacts.get(&c).map_or(false, |c| !c.is_child() && !c.departed) { *votes.entry(c).or_default() += 1; } } }
            let mut cands: Vec<(u16, usize)> = votes.into_iter().filter(|(c, _)| !claimed.contains_key(c)).collect();
            cands.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| self.contacts[&a.0].first_seen.cmp(&self.contacts[&b.0].first_seen)));
            match cands.first() {
                Some(&(c, _)) => { claimed.insert(c, cl); }
                None => unclaimed.push(cl),
            }
        }
        // Per track: where it goes this tick.
        let mut assign: Vec<(u32, u16)> = Vec::new();
        let mut births: Vec<Vec<u32>> = Vec::new();
        let patience = cfg.t_split;
        let mut place = |tracks: &mut BTreeMap<u32, TrackState>, contacts: &BTreeMap<u16, Contact>, m: u32, target: Option<u16>, newborn: &mut Vec<u32>| {
            let t = tracks.get_mut(&m).unwrap();
            let cur = t.contact.filter(|c| contacts.get(c).map_or(false, |c| !c.is_child() && !c.departed));
            match (cur, target) {
                (Some(cur), Some(tg)) if cur == tg => { t.outside_since = None; assign.push((m, tg)); }
                (Some(cur), _) => {
                    let since = *t.outside_since.get_or_insert(now);
                    if now - since < patience { assign.push((m, cur)); }
                    else { t.outside_since = None; match target { Some(tg) => assign.push((m, tg)), None => newborn.push(m) } }
                }
                (None, Some(tg)) => { t.outside_since = None; assign.push((m, tg)); }
                (None, None) => { t.outside_since = None; newborn.push(m); }
            }
        };
        for (cid, members) in &claimed { let mut nb = Vec::new(); for &m in members { place(&mut self.tracks, &self.contacts, m, Some(*cid), &mut nb); } debug_assert!(nb.is_empty()); }
        for cl in &unclaimed { let mut nb = Vec::new(); for &m in cl { place(&mut self.tracks, &self.contacts, m, None, &mut nb); } if !nb.is_empty() { births.push(nb); } }
        for nb in births {
            let id = self.alloc_id();
            let mut c = Contact::new(id, now);
            c.first_seen = nb.iter().map(|t| self.tracks[t].first_seen).min().unwrap_or(now);
            self.contacts.insert(id, c);
            self.stats.births += 1;
            for m in nb { assign.push((m, id)); }
        }
        // Rebuild memberships: live clustered tracks as assigned; lost tracks stay where they were.
        for c in self.contacts.values_mut() { if !c.is_child() { c.members.retain(|m| self.tracks.get(m).map_or(false, |t| t.lost)); } }
        for (m, cid) in assign {
            if let Some(t) = self.tracks.get_mut(&m) { t.contact = Some(cid); }
            if let Some(c) = self.contacts.get_mut(&cid) { c.members.push(m); }
        }
        // Confirmed lost tracks whose contact vanished get their own, so a lost object is still
        // reported as lost; a track lost before confirmation just goes.
        let orphans: Vec<u32> = self.tracks.values().filter(|t| t.lost && t.confirmed(&cfg, now) && t.contact.map_or(true, |c| !self.contacts.contains_key(&c))).map(|t| t.id).collect();
        for id in orphans {
            let cid = self.alloc_id();
            let mut c = Contact::new(cid, now);
            c.first_seen = self.tracks[&id].first_seen;
            c.members.push(id);
            self.contacts.insert(cid, c);
            self.tracks.get_mut(&id).unwrap().contact = Some(cid);
        }
        let dead: Vec<u32> = self.tracks.values().filter(|t| t.lost && t.contact.map_or(true, |c| !self.contacts.contains_key(&c))).map(|t| t.id).collect();
        for id in dead { self.tracks.remove(&id); }
    }

    /// Children of split-focused groups: one contact per member, with `parent`.
    fn children(&mut self, now: u32) {
        let parents: Vec<(u16, Vec<u32>)> = self.contacts.values().filter(|c| c.focused && c.split && !c.departed && !c.is_child()).map(|c| (c.id, c.members.clone())).collect();
        let mut live_children: Vec<u16> = Vec::new();
        for (pid, members) in parents {
            for m in members {
                let cid = match self.child_ids.get(&m) { Some(&c) => c, None => { let c = self.alloc_id(); self.child_ids.insert(m, c); c } };
                let c = self.contacts.entry(cid).or_insert_with(|| { let mut c = Contact::new(cid, now); c.first_seen = now; c });
                c.parent = Some(pid); c.members = vec![m]; c.departed = false; c.departed_at = None;
                c.first_seen = self.tracks.get(&m).map_or(c.first_seen, |t| t.first_seen);
                live_children.push(cid);
            }
        }
        for c in self.contacts.values_mut() {
            if c.is_child() && !live_children.contains(&c.id) && !c.departed { c.departed = true; c.departed_at = Some(now); c.members.clear(); }
        }
    }

    fn recompute(&mut self, now: u32) {
        let cfg = self.cfg;
        let mut departed_now = Vec::new();
        for c in self.contacts.values_mut() {
            if c.departed { continue; }
            let members: Vec<&TrackState> = c.members.iter().filter_map(|m| self.tracks.get(m)).collect();
            if members.is_empty() {
                c.departed = true; c.departed_at = Some(now); c.lost = true; departed_now.push(c.id);
                continue;
            }
            let k = members.len() as f32;
            c.e = members.iter().map(|t| t.e).sum::<f32>() / k;
            c.n = members.iter().map(|t| t.n).sum::<f32>() / k;
            c.radius = members.iter().map(|t| ((t.e - c.e).powi(2) + (t.n - c.n).powi(2)).sqrt()).fold(0.0, f32::max);
            c.ce = members.iter().map(|t| t.ce).fold(0.0, f32::max);
            c.conf = (members.iter().map(|t| t.conf as u32).sum::<u32>() / members.len() as u32) as u8;
            c.mix = [0; 4];
            for t in &members { let i = coarse(t.class) as usize; c.mix[i] = c.mix[i].saturating_add(1); }
            c.first_seen = c.first_seen.min(members.iter().map(|t| t.first_seen).min().unwrap());
            c.last_seen = members.iter().map(|t| t.last_seen).max().unwrap();
            c.confirmed = members.iter().any(|t| t.confirmed(&cfg, now));
            c.lost = members.iter().all(|t| t.lost);
            c.bbox = union_bbox(members.iter().filter_map(|t| t.bbox));
            let moving: Vec<&&TrackState> = members.iter().filter(|t| t.motion == MOTION_MOVING).collect();
            let stopped = members.iter().filter(|t| t.motion == MOTION_STOPPED).count();
            let statics = members.iter().filter(|t| t.motion == MOTION_STATIC).count();
            let next = if moving.len() * 2 > members.len() || (!moving.is_empty() && moving.len() >= stopped + statics) { MOTION_MOVING }
                else if stopped > 0 && stopped >= statics { MOTION_STOPPED }
                else if statics > 0 { MOTION_STATIC } else { MOTION_UNKNOWN };
            if next != c.motion {
                c.motion = next;
                c.since = members.iter().filter(|t| t.motion == next).map(|t| t.since).min().unwrap_or(now);
            }
            if !moving.is_empty() {
                let km = moving.len() as f32;
                c.ve = moving.iter().map(|t| t.ve).sum::<f32>() / km;
                c.vn = moving.iter().map(|t| t.vn).sum::<f32>() / km;
            } else { c.ve = 0.0; c.vn = 0.0; }
            let (crs, spd) = course_speed(c.ve, c.vn);
            c.course = crs; c.speed = spd;
        }
        self.stats.departures += departed_now.len() as u32;
        self.stats.contacts = self.contacts.values().filter(|c| !c.departed && !c.is_child()).count() as u32;
        self.stats.groups = self.contacts.values().filter(|c| !c.departed && !c.is_child() && c.is_group()).count() as u32;
    }

    fn detect_changes(&mut self, now: u32) -> Vec<u16> {
        let mut changed = Vec::new();
        let pos_res = self.pos_res_m;
        for c in self.contacts.values_mut() {
            if c.dirty { continue; }
            let s = match c.sent { Some(s) => s, None => { c.bump(now); changed.push(c.id); self.stats.rev_why[0] += 1; continue; } };
            let mut why: Option<usize> = None;
            if s.motion != c.motion || s.confirmed != c.confirmed || s.lost != c.lost || s.departed != c.departed || s.mix != c.mix { why = Some(1); }
            if why.is_none() {
                let (ge, gn) = c.ghost(now).unwrap();
                let dev = ((c.e - ge).powi(2) + (c.n - gn).powi(2)).sqrt();
                let thr = c.ce.max(2.0 * pos_res) * self.cfg.dev_factor * if c.focused { 0.5 } else { 1.0 };
                if dev > thr { why = Some(2); }
            }
            if why.is_none() && (c.ce > s.ce * 1.5 || c.ce < s.ce / 1.5) && (c.ce - s.ce).abs() > 2.0 * pos_res { why = Some(3); }
            if why.is_none() && c.motion == MOTION_MOVING {
                let mut dc = (c.course - s.course).abs(); if dc > 180.0 { dc = 360.0 - dc; }
                if dc > 30.0 { why = Some(4); } else if (c.speed - s.speed).abs() > 0.25 * s.speed.max(0.5) { why = Some(5); }
            }
            if let Some(w) = why { c.bump(now); changed.push(c.id); self.stats.rev_why[w] += 1; }
        }
        self.stats.revisions += changed.len() as u64;
        changed
    }

    /// The scheduler emitted this contact's record at `now`: what it carried is what the receiver
    /// will believe.
    pub fn mark_sent(&mut self, id: u16, now: u32) {
        if let Some(c) = self.contacts.get_mut(&id) { c.sent = Some(c.snapshot(now)); c.dirty = false; }
    }

    pub fn remove(&mut self, id: u16) {
        if let Some(c) = self.contacts.remove(&id) {
            for m in c.members { if let Some(t) = self.tracks.get_mut(&m) { if t.contact == Some(id) { t.contact = None; } } }
            self.child_ids.retain(|_, &mut v| v != id);
        }
    }

    pub fn set_focus(&mut self, id: u16, track: bool, split: bool) -> bool {
        match self.contacts.get_mut(&id) {
            Some(c) if !c.departed => { c.focused = track || split; c.split = split; true }
            _ => false,
        }
    }

    /// Live, top-level contacts (what `Ego.n_*` counts).
    pub fn summary(&self) -> (u8, u8, [u8; 4]) {
        let mut n = 0u32; let mut moving = 0u32; let mut mix = [0u32; 4];
        for c in self.contacts.values().filter(|c| !c.departed && !c.is_child() && !c.lost) {
            n += 1; if c.moving() { moving += 1; }
            for i in 0..4 { mix[i] += c.mix[i] as u32; }
        }
        let sat = |x: u32| x.min(255) as u8;
        (sat(n), sat(moving), [sat(mix[0]), sat(mix[1]), sat(mix[2]), sat(mix[3])])
    }
}

impl Contact {
    fn bump(&mut self, now: u32) { self.rev = self.rev.wrapping_add(1); self.dirty = true; self.rev_tick = now; }
}

fn union_bbox(boxes: impl Iterator<Item = [f32; 4]>) -> Option<[f32; 4]> {
    let mut acc: Option<(f32, f32, f32, f32)> = None;
    for b in boxes {
        let (x0, y0, x1, y1) = (b[0] - b[2] / 2.0, b[1] - b[3] / 2.0, b[0] + b[2] / 2.0, b[1] + b[3] / 2.0);
        acc = Some(match acc { None => (x0, y0, x1, y1), Some(a) => (a.0.min(x0), a.1.min(y0), a.2.max(x1), a.3.max(y1)) });
    }
    acc.map(|(x0, y0, x1, y1)| [(x0 + x1) / 2.0, (y0 + y1) / 2.0, x1 - x0, y1 - y0])
}

pub fn coarse_is_vehicle(c: u8) -> bool { c == COARSE_VEHICLE || c == COARSE_ARMOUR }
pub fn coarse_is_dismount(c: u8) -> bool { c == COARSE_DISMOUNT }

#[cfg(test)]
mod tests {
    use super::*;
    use crate::classes::{CAR, PERSON};

    fn tr(id: u32, class: u8, e: f32, n: f32, ve: f32, vn: f32) -> Track { Track { id, class, e, n, ve, vn, conf: 200, ce: Some(5.0), bbox: None } }

    fn run(cm: &mut ContactManager, scene: impl Fn(u32) -> Vec<Track>, from: u32, to: u32, step: u32) -> Vec<(u32, Vec<u16>)> {
        let mut out = Vec::new();
        let mut t = from;
        while t <= to { let ch = cm.update(&scene(t), t); if !ch.is_empty() { out.push((t, ch)); } t += step; }
        out
    }

    #[test]
    fn a_parked_car_becomes_one_static_contact_and_one_revision() {
        let mut cm = ContactManager::new(ContactConfig::default(), 1.0);
        let changes = run(&mut cm, |_| vec![tr(1, CAR, 10.0, 20.0, 0.0, 0.0)], 0, 20 * TICK_HZ, 12);
        assert_eq!(cm.contacts.len(), 1);
        let c = cm.contacts.values().next().unwrap();
        assert_eq!(c.motion, MOTION_STATIC);
        assert_eq!(c.mix, [0, 1, 0, 0]);
        assert!(c.confirmed);
        // Birth (rev 1), confirmed, then static: at most three revisions in 20 s, none after.
        assert!(changes.len() <= 3, "{changes:?}");
        // Simulate sends so change detection has a baseline, then nothing should change.
        let id = c.id;
        cm.mark_sent(id, 20 * TICK_HZ);
        let later = run(&mut cm, |_| vec![tr(1, CAR, 10.3, 19.8, 0.0, 0.0)], 20 * TICK_HZ + 12, 60 * TICK_HZ, 12);
        assert!(later.is_empty(), "a static car within ce is never revised: {later:?}");
    }

    #[test]
    fn a_convoy_is_one_group_and_a_split_car_leaves_it() {
        let mut cm = ContactManager::new(ContactConfig::default(), 1.0);
        let convoy = |t: u32| {
            let s = t as f32 / TICK_HZ as f32;
            (0..4).map(|i| tr(10 + i, CAR, 8.0 * s + 10.0 * i as f32, 0.0, 8.0, 0.0)).collect::<Vec<_>>()
        };
        run(&mut cm, convoy, 0, 10 * TICK_HZ, 12);
        let groups: Vec<&Contact> = cm.contacts.values().filter(|c| !c.departed).collect();
        assert_eq!(groups.len(), 1, "{:?}", groups.iter().map(|c| (c.id, c.members.clone())).collect::<Vec<_>>());
        let g = groups[0];
        assert_eq!(g.count(), 4); assert!(g.is_group()); assert_eq!(g.motion, MOTION_MOVING);
        assert!((g.speed - 8.0).abs() < 0.1 && (g.course - 90.0).abs() < 1.0);
        assert!(g.radius > 14.0 && g.radius < 16.0, "{}", g.radius);
        let gid = g.id;
        // Car 13 stops while the others go on: it leaves the group after the hysteresis.
        let split = |t: u32| {
            let s = t as f32 / TICK_HZ as f32;
            let mut v: Vec<Track> = (0..3).map(|i| tr(10 + i, CAR, 8.0 * s + 10.0 * i as f32, 0.0, 8.0, 0.0)).collect();
            v.push(tr(13, CAR, 110.0, 0.0, 0.0, 0.0));
            v
        };
        run(&mut cm, split, 10 * TICK_HZ + 12, 30 * TICK_HZ, 12);
        let live: Vec<&Contact> = cm.contacts.values().filter(|c| !c.departed).collect();
        assert_eq!(live.len(), 2, "{:?}", live.iter().map(|c| (c.id, c.members.clone(), c.motion)).collect::<Vec<_>>());
        let g = &cm.contacts[&gid];
        assert_eq!(g.count(), 3, "the convoy keeps its id and loses one member");
        let solo = live.iter().find(|c| c.id != gid).unwrap();
        assert_eq!(solo.members, vec![13]);
        assert!(solo.motion == MOTION_STOPPED || solo.motion == MOTION_STATIC);
    }

    #[test]
    fn lost_then_departed_with_tombstone() {
        let mut cm = ContactManager::new(ContactConfig::default(), 1.0);
        run(&mut cm, |_| vec![tr(5, PERSON, 0.0, 0.0, 0.0, 0.0)], 0, 5 * TICK_HZ, 12);
        let id = *cm.contacts.keys().next().unwrap();
        cm.mark_sent(id, 5 * TICK_HZ);
        // Silence: lost after 5 s, departed after 60 s.
        let ch = run(&mut cm, |_| vec![], 5 * TICK_HZ + 12, 12 * TICK_HZ, 12);
        assert!(cm.contacts[&id].lost);
        assert!(ch.iter().any(|(_, ids)| ids.contains(&id)), "lost is a revision");
        run(&mut cm, |_| vec![], 12 * TICK_HZ + 12, 70 * TICK_HZ, 12);
        let c = &cm.contacts[&id];
        assert!(c.departed && c.departed_at.is_some());
        assert!(cm.tracks.is_empty());
        assert_eq!(cm.summary().0, 0, "tombstones are not counted");
    }

    #[test]
    fn motion_machine_times_the_transition_from_the_evidence() {
        let mut cm = ContactManager::new(ContactConfig::default(), 1.0);
        // Static for 10 s, then moving at 2 m/s.
        let scene = |t: u32| { let s = t as f32 / TICK_HZ as f32; if s < 10.0 { vec![tr(1, CAR, 0.0, 0.0, 0.0, 0.0)] } else { vec![tr(1, CAR, 2.0 * (s - 10.0), 0.0, 2.0, 0.0)] } };
        run(&mut cm, scene, 0, 15 * TICK_HZ, 12);
        let c = cm.contacts.values().next().unwrap();
        assert_eq!(c.motion, MOTION_MOVING);
        let since_s = c.since as f32 / TICK_HZ as f32;
        assert!((since_s - 10.0).abs() < 0.2, "moving since {since_s} s, expected ~10 s (the evidence), not 12 (the timer)");
        assert!(c.has_velocity_for_wire());
    }

    #[test]
    fn a_pedestrian_near_a_moving_car_is_not_grouped_with_it() {
        let mut cm = ContactManager::new(ContactConfig::default(), 1.0);
        let scene = |t: u32| { let s = t as f32 / TICK_HZ as f32; vec![tr(1, CAR, 10.0 * s, 0.0, 10.0, 0.0), tr(2, PERSON, 50.0, 3.0, 0.0, 0.0)] };
        run(&mut cm, scene, 0, 10 * TICK_HZ, 12);
        assert_eq!(cm.contacts.values().filter(|c| !c.departed).count(), 2);
    }

    #[test]
    fn a_flickering_crowd_keeps_its_id() {
        let mut cm = ContactManager::new(ContactConfig::default(), 1.0);
        // Six pedestrians; every other second one of them steps 10 m out and back (beyond the 8 m
        // dismount link), which without patience would mint a new contact each time.
        let scene = |t: u32| {
            let s = t as f32 / TICK_HZ as f32;
            let out = ((s as u32) % 2 == 1) && s > 5.0;
            (0..6).map(|i| tr(i, PERSON, 2.0 * i as f32 + if i == 5 && out { 12.0 } else { 0.0 }, 0.0, 0.0, 0.0)).collect::<Vec<_>>()
        };
        run(&mut cm, scene, 0, 30 * TICK_HZ, 12);
        let live: Vec<&Contact> = cm.contacts.values().filter(|c| !c.departed).collect();
        assert_eq!(live.len(), 1, "{:?}", live.iter().map(|c| (c.id, c.members.clone())).collect::<Vec<_>>());
        assert!(cm.stats.births <= 2, "births {}", cm.stats.births);
        // A real split (out for good) does produce a second contact after the patience.
        let scene2 = |_t: u32| (0..6).map(|i| tr(i, PERSON, 2.0 * i as f32 + if i == 5 { 30.0 } else { 0.0 }, 0.0, 0.0, 0.0)).collect::<Vec<_>>();
        run(&mut cm, scene2, 30 * TICK_HZ + 12, 40 * TICK_HZ, 12);
        let live: Vec<&Contact> = cm.contacts.values().filter(|c| !c.departed).collect();
        assert_eq!(live.len(), 2);
    }

    impl Contact { pub fn has_velocity_for_wire(&self) -> bool { self.motion == MOTION_MOVING && self.speed > 0.0 } }
}
