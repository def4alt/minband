//! The edge: tracks and own state in, frames out. Owns the contact manager, the schedule of every
//! record, the pose queue, the token bucket for the budget, and the frame builder.

use crate::contacts::{Contact, ContactConfig, ContactManager, Track};
use crate::geo::{ce_m, ray_az_el};
use crate::scheduler::{target_frame_bytes, timing, Entry, Regime, Timing, LADDER_LEN};
use crate::wire::*;
use crate::TICK_HZ;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct EdgeConfig {
    pub device_id: u16,
    pub nonce: u32,
    pub origin_lat_e7: i32,
    pub origin_lon_e7: i32,
    pub origin_alt: i16,
    pub pos_res: u8,
    pub caps: u16,
    pub utc_at_tick0: u32,
    pub hfov_x10: u16,
    pub img_w: u16,
    pub img_h: u16,
    pub video_frame0: u32,
    pub fps_x100: u16,
    /// bit/s on the link, header included; 0 = unlimited.
    pub budget_bps: u32,
    pub max_frame: usize,
    pub crc: bool,
    pub carrier_overhead: usize,
    pub sigma_own: f32,
    pub sigma_att_deg: f32,
    pub sigma_h: f32,
    pub sigma_px: f32,
    pub f_px: f32,
    pub contacts: ContactConfig,
}
impl Default for EdgeConfig {
    fn default() -> Self {
        EdgeConfig { device_id: 1, nonce: 1, origin_lat_e7: 0, origin_lon_e7: 0, origin_alt: 0x7FFF, pos_res: 2, caps: CAP_GNSS | CAP_BARO | CAP_MAG | CAP_IMU,
            utc_at_tick0: 0, hfov_x10: 850, img_w: 1920, img_h: 1080, video_frame0: 0xFFFF_FFFF, fps_x100: 0, budget_bps: 800, max_frame: 1200, crc: false,
            carrier_overhead: UDP_IP_OVERHEAD, sigma_own: 3.0, sigma_att_deg: 1.5, sigma_h: 2.0, sigma_px: 2.0, f_px: 2000.0, contacts: ContactConfig::default() }
    }
}

/// Own state per tick (what the flight controller or the phone knows).
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct EgoInput {
    pub e: f32, pub n: f32, pub alt_agl: f32,
    pub heading_deg: f32, pub speed: f32, pub climb: f32,
    pub nav_mode: u8, pub gnss: u8, pub battery: u8, pub pos_ce: f32,
    pub fp_e: f32, pub fp_n: f32, pub fp_radius: f32,
    pub video: bool,
    /// The camera is delivering frames. When it stops (gimbal away, feed lost, end of a replay)
    /// what goes lost afterwards has left the view, it was not lost in it.
    pub looking: bool,
}
impl Default for EgoInput {
    fn default() -> Self { EgoInput { e: 0.0, n: 0.0, alt_agl: 0.0, heading_deg: 0.0, speed: 0.0, climb: 0.0, nav_mode: 2, gnss: 2, battery: 255, pos_ce: 3.0, fp_e: 0.0, fp_n: 0.0, fp_radius: 0.0, video: false, looking: true } }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct EdgeStats {
    pub seq: u16, pub frames: u64, pub bytes: u64, pub bytes_wire: u64,
    pub contacts_sent: u64, pub ego_sent: u64, pub session_sent: u64, pub pose_sent: u64,
    pub budget_bps: u32, pub regime: Option<Regime>, pub digests: u64, pub focus_cmds: u64, pub tokens: f32,
}

#[derive(Clone, Debug, Serialize)]
struct Focus { until: u32, split: bool, chip: bool }

pub struct Edge {
    pub cfg: EdgeConfig,
    pub cm: ContactManager,
    timing: Timing,
    entries: BTreeMap<u16, Entry>,
    ego_due: u32,
    last_nav: Option<u8>,
    session_due: u32,
    session_sends: u32,
    poses: Vec<PoseRec>,
    last_pose_tick: Option<u32>,
    focus: BTreeMap<u16, Focus>,
    tokens: f32,
    last_tick: Option<u32>,
    last_frame_tick: Option<u32>,
    pub stats: EdgeStats,
    last_ego: Option<EgoRec>,
    uplink_heard: Option<u32>,
}

impl Edge {
    pub fn new(cfg: EdgeConfig) -> Self {
        let t = timing(cfg.budget_bps);
        let cm = ContactManager::new(cfg.contacts, pos_res_m(cfg.pos_res));
        Edge { timing: t, cm, entries: BTreeMap::new(), ego_due: 0, last_nav: None, session_due: 0, session_sends: 0, poses: Vec::new(), last_pose_tick: None,
            focus: BTreeMap::new(), tokens: 0.0, last_tick: None, last_frame_tick: None, stats: EdgeStats { budget_bps: cfg.budget_bps, regime: Some(t.regime), ..Default::default() },
            last_ego: None, uplink_heard: None, cfg }
    }

    pub fn timing(&self) -> &Timing { &self.timing }

    pub fn set_budget(&mut self, bps: u32) {
        if bps == self.cfg.budget_bps { return; }
        self.cfg.budget_bps = bps;
        self.timing = timing(bps);
        self.stats.budget_bps = bps; self.stats.regime = Some(self.timing.regime);
        self.tokens = self.tokens.min(2.0 * target_frame_bytes(bps, self.cfg.max_frame) as f32);
    }

    /// A camera pose at a video frame's tick, queued for the `Pose` stream (video and wide regimes).
    pub fn pose(&mut self, tick: u32, e: f32, n: f32, up: f32, yaw_deg: f32, pitch_deg: f32, roll_deg: f32) {
        let Some(every) = self.timing.pose else { return };
        if let Some(last) = self.last_pose_tick { if tick.saturating_sub(last) < every { return; } }
        self.last_pose_tick = Some(tick);
        let cd = |d: f32| (d * 100.0).round().clamp(-32768.0, 32767.0) as i16;
        let cm = |m: f32| (m * 100.0).round().clamp(i32::MIN as f32, i32::MAX as f32) as i32;
        self.poses.push(PoseRec { tick, x: cm(e), y: cm(n), z: cm(up), yaw: cd(yaw_deg), pitch: cd(pitch_deg), roll: cd(roll_deg) });
        if self.poses.len() > 20 { self.poses.remove(0); }
    }

    /// Uplink frames (Digest, Focus, Clock, ChipAck).
    pub fn on_uplink(&mut self, bytes: &[u8], now: u32) -> Result<(), CodecError> {
        let f = Frame::decode(bytes)?;
        if !f.uplink { return Ok(()); }
        self.uplink_heard = Some(now);
        for r in f.records {
            match r {
                Record::Digest(d) => {
                    self.stats.digests += 1;
                    let budget = d.budget_10bps as u32 * 10;
                    self.set_budget(budget);
                    let t = self.timing;
                    for (id, rev) in d.acked {
                        let Some(c) = self.cm.contacts.get(&id) else { continue };
                        // A focused record keeps its T_focus cadence: the ack says the receiver has this
                        // revision, but focus is about fresh observations of it, not about delivery.
                        if c.rev != rev || c.dirty || c.fast() { continue; }
                        if c.departed { self.entries.remove(&id); self.cm.remove(id); continue; }
                        if let Some(e) = self.entries.get_mut(&id) { e.acked(&t); }
                    }
                }
                Record::Focus(fc) => {
                    self.stats.focus_cmds += 1;
                    if fc.mode & FOCUS_RELEASE != 0 {
                        // Re-send at once so the receiver drops the focused flag now, not at the floor.
                        self.focus.remove(&fc.id); self.cm.set_focus(fc.id, false, false);
                        if let Some(e) = self.entries.get_mut(&fc.id) { e.changed(now); }
                        continue;
                    }
                    let ttl = if fc.ttl == 0 { 60 } else { fc.ttl } as u32 * TICK_HZ;
                    let split = fc.mode & FOCUS_SPLIT != 0;
                    if self.cm.set_focus(fc.id, fc.mode & FOCUS_TRACK != 0 || split, split) {
                        let entry = self.focus.entry(fc.id).or_insert(Focus { until: now + ttl, split, chip: false });
                        entry.until = now + ttl; entry.split |= split; entry.chip |= fc.mode & FOCUS_CHIP != 0;
                        if self.focus.len() > 4 { let oldest = *self.focus.iter().min_by_key(|(_, f)| f.until).map(|(k, _)| k).unwrap(); self.focus.remove(&oldest); self.cm.set_focus(oldest, false, false); }
                        if let Some(e) = self.entries.get_mut(&fc.id) { e.due = now; }
                    }
                }
                Record::Clock { utc } => { if self.cfg.utc_at_tick0 == 0 { self.cfg.utc_at_tick0 = utc.saturating_sub(now / TICK_HZ); } }
                _ => {}
            }
        }
        Ok(())
    }

    /// One tick: ingest tracks and own state, schedule, build frames. `tracks` may be empty on
    /// ticks without a detector output; call at the tracker rate (5-30 Hz).
    pub fn tick(&mut self, tracks: &[Track], ego: &EgoInput, now: u32) -> Vec<Vec<u8>> {
        // Fill in ce from the geometry for tracks that carry none.
        let filled: Vec<Track> = tracks.iter().map(|t| {
            if t.ce.is_some() { return *t; }
            let (_, el) = ray_az_el(ego.e, ego.n, ego.alt_agl, t.e, t.n, 0.0);
            let range = ((t.e - ego.e).powi(2) + (t.n - ego.n).powi(2)).sqrt();
            let mut tt = *t;
            tt.ce = Some(ce_m(range, el, ego.pos_ce.max(self.cfg.sigma_own), self.cfg.sigma_att_deg, self.cfg.sigma_h, self.cfg.sigma_px, self.cfg.f_px));
            tt
        }).collect();
        // Focus expiry.
        let expired: Vec<u16> = self.focus.iter().filter(|(_, f)| now >= f.until).map(|(k, _)| *k).collect();
        for id in expired { self.focus.remove(&id); self.cm.set_focus(id, false, false); if let Some(e) = self.entries.get_mut(&id) { e.changed(now); } }

        if self.cm.looking && !ego.looking { self.cm.blind_since = Some(now); }
        if ego.looking { self.cm.blind_since = None; }
        self.cm.looking = ego.looking;
        let changed = self.cm.update(&filled, now);
        // A focused revision goes out as soon as the focus share allows: at once on a fast link,
        // after `focus_share_gap` on a thin one. Under focus the change threshold is halved, and a
        // split group of walking people would otherwise revise every step and starve the picture.
        let gap = self.focus_share_gap();
        for id in changed {
            let fast = self.cm.contacts.get(&id).map_or(false, |c| c.fast() && !c.departed && !c.lost);
            let e = self.entries.entry(id).or_insert_with(Entry::default);
            match (fast, e.last_sent) {
                (true, Some(last)) => { e.step = 0; e.due = e.due.min(now.max(last + gap)); }
                _ => e.changed(now),
            }
        }
        for id in self.cm.contacts.keys() { self.entries.entry(*id).or_insert_with(|| { let mut e = Entry::default(); e.changed(now); e }); }
        // Tombstones past their life leave the rotation.
        let t = self.timing;
        let drop: Vec<u16> = self.cm.contacts.values().filter(|c| c.departed && c.departed_at.map_or(false, |d| now.saturating_sub(d) > 2 * t.floor) && !c.dirty).map(|c| c.id).collect();
        for id in drop { self.entries.remove(&id); self.cm.remove(id); }

        // Ego: on schedule, and at once when the nav byte changes.
        let nav = EgoRec::nav_byte(ego.nav_mode, ego.gnss, self.link_state(now), ego.video);
        if self.last_nav.map_or(true, |n| n != nav) { self.ego_due = now; }
        self.last_nav = Some(nav);

        // Token bucket for the budget.
        let dt = self.last_tick.map_or(0, |l| now.saturating_sub(l)) as f32 / TICK_HZ as f32;
        self.last_tick = Some(now);
        let target = target_frame_bytes(self.cfg.budget_bps, self.cfg.max_frame);
        if self.cfg.budget_bps > 0 {
            self.tokens = (self.tokens + dt * self.cfg.budget_bps as f32 / 8.0).min(2.0 * target as f32);
        }
        let mut out = Vec::new();
        loop {
            let unlimited = self.cfg.budget_bps == 0;
            if unlimited { if self.last_frame_tick.map_or(false, |l| now.saturating_sub(l) < TICK_HZ / 10) { break; } }
            else if self.tokens < target as f32 { break; }
            let Some(frame) = self.build_frame(ego, nav, now, target) else { break };
            let bytes = frame.encode(self.cfg.crc);
            self.tokens -= (bytes.len() + self.cfg.carrier_overhead) as f32;
            self.stats.frames += 1; self.stats.bytes += bytes.len() as u64; self.stats.bytes_wire += (bytes.len() + self.cfg.carrier_overhead) as u64;
            self.last_frame_tick = Some(now);
            out.push(bytes);
            if unlimited { break; }
        }
        self.stats.tokens = self.tokens;
        out
    }

    fn link_state(&self, now: u32) -> u8 {
        match self.uplink_heard { None => 2, Some(t) if now.saturating_sub(t) > 30 * TICK_HZ => 1, _ => 0 }
    }


    /// T_focus for this frame. Focused records take at most `FOCUS_SHARE` of the link: when more
    /// are focused (a split group of four, say) the period stretches so the rest of the picture keeps
    /// the other half. One focused contact never stretches it on the thin profiles (PROTOCOL.md 6.2).
    fn focus_period(&self) -> u32 { self.timing.focus.max(self.focus_share_gap()) }

    /// The shortest gap between two sends of one focused record that keeps all focused records
    /// within `FOCUS_SHARE` of the link (0 on an unlimited link).
    fn focus_share_gap(&self) -> u32 {
        if self.cfg.budget_bps == 0 { return 0; }
        let n = self.cm.contacts.values().filter(|c| c.fast() && !c.departed).count() as f32;
        if n == 0.0 { return 0; }
        let target = target_frame_bytes(self.cfg.budget_bps, self.cfg.max_frame) as f32;
        let app_bytes_per_s = self.cfg.budget_bps as f32 / 8.0 * target / (target + self.cfg.carrier_overhead as f32);
        let gap_s = n * FOCUS_RECORD_B / (FOCUS_SHARE * app_bytes_per_s);
        (gap_s * TICK_HZ as f32).ceil() as u32
    }

    fn build_frame(&mut self, ego: &EgoInput, nav: u8, now: u32, target: usize) -> Option<Frame> {
        let mut t = self.timing;
        t.focus = self.focus_period();
        let header = HEADER_LEN + if self.cfg.crc { CRC_LEN } else { 0 };
        let room = target.max(header + 1).min(self.cfg.max_frame);
        // Candidates: (class rank, ladder step, -overdue, kind). Within the ladder class the step
        // comes before the overdue time: a copy that has never been sent is worth more than the
        // second or third copy of something else (the receiver already has it with probability
        // 1 - p^k), so under saturation fresh news goes first and repeats fill what is left.
        #[derive(Clone, Copy)] enum K { Contact(u16), Ego, Session, Pose }
        let mut cands: Vec<(u8, u8, i64, K)> = Vec::new();
        for (id, e) in &self.entries {
            if e.due > now { continue; }
            let Some(c) = self.cm.contacts.get(id) else { continue };
            let rank = if c.fast() { 0 } else if e.step < LADDER_LEN { 2 } else if c.departed { 6 } else { 5 };
            cands.push((rank, e.step, -e.overdue(now), K::Contact(*id)));
        }
        if now >= self.ego_due { cands.push((1, 0, -(now as i64 - self.ego_due as i64), K::Ego)); }
        if now >= self.session_due { cands.push((1, 0, -(now as i64 - self.session_due as i64), K::Session)); }
        if !self.poses.is_empty() { cands.push((4, 0, 0, K::Pose)); }
        if cands.is_empty() { return None; }
        cands.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));

        let mut records: Vec<Record> = Vec::new();
        let mut len = header;
        let mut sent_ids: Vec<u16> = Vec::new();
        let mut sent_ego = false; let mut sent_session = false;
        for (_, _, _, k) in cands {
            match k {
                K::Contact(id) => {
                    let c = &self.cm.contacts[&id];
                    let rec = self.contact_rec(c, ego, now);
                    let n = Record::Contact(rec).wire_len();
                    if len + n > room && !records.is_empty() { continue; }
                    if len + n > self.cfg.max_frame { continue; }
                    records.push(Record::Contact(rec)); len += n; sent_ids.push(id);
                }
                K::Ego => {
                    let rec = self.ego_rec(ego, nav);
                    let n = Record::Ego(rec).wire_len();
                    if len + n > room && !records.is_empty() { continue; }
                    records.push(Record::Ego(rec)); len += n; sent_ego = true;
                }
                K::Session => {
                    let rec = self.session_rec();
                    let n = Record::Session(rec).wire_len();
                    if len + n > room && !records.is_empty() { continue; }
                    records.push(Record::Session(rec)); len += n; sent_session = true;
                }
                K::Pose => {
                    while let Some(p) = self.poses.first().copied() {
                        let n = Record::Pose(p).wire_len();
                        if len + n > room && !records.is_empty() { break; }
                        if len + n > self.cfg.max_frame { break; }
                        records.push(Record::Pose(p)); len += n; self.poses.remove(0); self.stats.pose_sent += 1;
                    }
                }
            }
        }
        if records.is_empty() { return None; }
        for id in &sent_ids {
            let fast = self.cm.contacts[id].fast();
            self.entries.get_mut(id).unwrap().sent(now, &t, fast);
            self.cm.mark_sent(*id, now);
            self.stats.contacts_sent += 1;
        }
        if sent_ego { self.ego_due = now + t.ego; self.stats.ego_sent += 1; }
        if sent_session {
            self.session_sends += 1;
            self.session_due = now + if self.session_sends < 3 { 4 * TICK_HZ } else { t.session };
            self.stats.session_sent += 1;
        }
        let cycle_end = self.entries.values().all(|e| e.due > now) && now < self.ego_due && now < self.session_due;
        let seq = self.stats.seq; self.stats.seq = seq.wrapping_add(1);
        Some(Frame { session: self.cfg.nonce as u16, seq, tick: now, uplink: false, cycle_end, records })
    }

    fn session_rec(&self) -> SessionRec {
        let c = &self.cfg;
        SessionRec { nonce: c.nonce, device_id: c.device_id, origin_lat: c.origin_lat_e7, origin_lon: c.origin_lon_e7, origin_alt: c.origin_alt, pos_res: c.pos_res,
            caps: c.caps, utc_at_tick0: c.utc_at_tick0, hfov_x10: c.hfov_x10, img_w: c.img_w, img_h: c.img_h, video_frame0: c.video_frame0, fps_x100: c.fps_x100 }
    }

    fn ego_rec(&mut self, ego: &EgoInput, nav: u8) -> EgoRec {
        let (n, moving, mix) = self.cm.summary();
        let r = self.cfg.pos_res;
        let rec = EgoRec { dx: m_to_pos(ego.e, r), dy: m_to_pos(ego.n, r), alt_agl: if ego.alt_agl.is_finite() { ego.alt_agl.round().clamp(-32768.0, 32766.0) as i16 } else { 0x7FFF },
            heading: deg_to_u8(ego.heading_deg), speed: speed_to_u8(ego.speed), climb: climb_to_i8(ego.climb), nav, battery: ego.battery, pos_ce: m_to_m8(ego.pos_ce),
            fp_dx: m_to_pos(ego.fp_e, r), fp_dy: m_to_pos(ego.fp_n, r), fp_radius: m_to_m8(ego.fp_radius),
            n_contacts: n, n_moving: moving, n_dismount: mix[0], n_vehicle: mix[1], n_armour: mix[2], n_other: mix[3] };
        self.last_ego = Some(rec);
        rec
    }

    fn contact_rec(&self, c: &Contact, ego: &EgoInput, now: u32) -> ContactRec {
        let r = self.cfg.pos_res;
        let mut flags = c.motion & F_MOTION_MASK;
        if c.confirmed { flags |= F_CONFIRMED; }
        if c.lost { flags |= F_LOST; }
        if c.departed { flags |= F_DEPARTED; }
        if c.focused { flags |= F_FOCUSED; }
        if c.is_group() { flags |= F_GROUP; }
        let has_vel = c.motion == MOTION_MOVING && c.speed > 0.0;
        if has_vel { flags |= F_VELOCITY; }
        let mut ext = 0u8;
        if c.parent.is_some() { ext |= X_PARENT | X_CHILD; }
        if c.out_of_view && c.lost && !c.departed { ext |= X_OUT_OF_VIEW; }
        let regime = self.timing.regime;
        let want_ray = !c.departed && matches!(regime, Regime::Video | Regime::Wide | Regime::Thin);
        let want_bbox = !c.departed && matches!(regime, Regime::Video | Regime::Wide) && c.bbox.is_some();
        if want_ray { ext |= X_RAY; }
        if want_bbox { ext |= X_BBOX; }
        let (az, el) = ray_az_el(ego.e, ego.n, ego.alt_agl, c.e, c.n, 0.0);
        let bb = c.bbox.unwrap_or([0.0; 4]);
        ContactRec { id: c.id, rev: c.rev, flags, ext, dx: m_to_pos(c.e, r), dy: m_to_pos(c.n, r), ce: m_to_m8(c.ce), radius: m_to_m8(c.radius),
            n_dismount: c.mix[0], n_vehicle: c.mix[1], n_armour: c.mix[2], n_other: c.mix[3], conf: c.conf,
            first_seen: secs_u16(c.first_seen), since: secs_u16(c.since), age: age_u8(now.saturating_sub(c.last_seen)),
            course: deg_to_u8(c.course), speed: speed_to_u8(c.speed), parent: c.parent.unwrap_or(0), dz: 0,
            az: deg_to_u8(az), el: el_to_u8(el), bbox: [nrm_to_u8(bb[0]), nrm_to_u8(bb[1]), nrm_to_u8(bb[2]), nrm_to_u8(bb[3])] }
    }

    /// The edge's own view, for the side-by-side page: contacts as it holds them.
    pub fn snapshot(&self, now: u32) -> EdgeSnapshot {
        let contacts = self.cm.contacts.values().map(|c| ContactView::from_contact(c, now, self.entries.get(&c.id))).collect();
        EdgeSnapshot { contacts, tracks: self.cm.tracks.values().map(|t| TrackView { id: t.id, class: t.class, e: t.e, n: t.n, ve: t.ve, vn: t.vn, conf: t.conf, ce: t.ce, lost: t.lost, contact: t.contact, motion: motion_name(t.motion) }).collect(),
            timing: self.timing, tokens: self.tokens, focus: self.focus.keys().copied().collect(), stats: self.cm.stats.clone() }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct TrackView { pub id: u32, pub class: u8, pub e: f32, pub n: f32, pub ve: f32, pub vn: f32, pub conf: u8, pub ce: f32, pub lost: bool, pub contact: Option<u16>, pub motion: &'static str }

#[derive(Clone, Debug, Serialize)]
pub struct ContactView {
    pub id: u16, pub rev: u8, pub e: f32, pub n: f32, pub ce: f32, pub radius: f32, pub count: u32, pub mix: [u8; 4],
    pub motion: &'static str, pub confirmed: bool, pub lost: bool, pub departed: bool, pub focused: bool, pub split: bool,
    pub course: f32, pub speed: f32, pub members: Vec<u32>, pub first_seen: f32, pub since: f32, pub parent: Option<u16>,
    pub dirty: bool, pub step: u8, pub due_in: f32, pub sends: u32, pub bbox: Option<[f32; 4]>,
}
impl ContactView {
    pub fn from_contact(c: &Contact, now: u32, e: Option<&Entry>) -> Self {
        let s = |t: u32| t as f32 / TICK_HZ as f32;
        ContactView { id: c.id, rev: c.rev, e: c.e, n: c.n, ce: c.ce, radius: c.radius, count: c.count(), mix: c.mix, motion: motion_name(c.motion), confirmed: c.confirmed,
            lost: c.lost, departed: c.departed, focused: c.focused, split: c.split, course: c.course, speed: c.speed, members: c.members.clone(), first_seen: s(c.first_seen),
            since: s(c.since), parent: c.parent, dirty: c.dirty, step: e.map_or(0, |e| e.step), due_in: e.map_or(0.0, |e| (e.due as i64 - now as i64) as f32 / TICK_HZ as f32), sends: e.map_or(0, |e| e.sends), bbox: c.bbox }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct EdgeSnapshot { pub contacts: Vec<ContactView>, pub tracks: Vec<TrackView>, pub timing: Timing, pub tokens: f32, pub focus: Vec<u16>, pub stats: crate::contacts::ContactStats }

/// Share of the link focused records may take, and the size assumed for one (a moving child with
/// its ray, framing included).
pub const FOCUS_SHARE: f32 = 0.5;
pub const FOCUS_RECORD_B: f32 = 30.0;

#[cfg(test)]
mod tests {
    use super::*;

    fn track(id: u32, e: f32) -> Track { Track { id, class: 2, e, n: 0.0, ve: 0.0, vn: 0.0, conf: 200, ce: Some(3.0), bbox: None } }
    /// Under saturation a never-sent revision goes before the repeats of older ones: six static
    /// contacts are due for their second copy (overdue 100 ticks), a newborn seventh is due for its
    /// first (overdue 10). A 100 B frame takes three contacts; the newborn must be one of them.
    #[test]
    fn fresh_revisions_go_before_repeats() {
        let cfg = EdgeConfig { budget_bps: 0, carrier_overhead: 0, ..Default::default() };
        let mut edge = Edge::new(cfg);
        let ego = EgoInput::default();
        let tracks: Vec<Track> = (1..=7).map(|i| track(i, i as f32 * 200.0)).collect();
        let mut now = 0;
        while now <= 120 { edge.tick(&tracks, &ego, now); now += 12; }
        assert_eq!(edge.cm.contacts.len(), 7);
        let seven = edge.cm.tracks[&7].contact.unwrap();
        let now = 2400;
        for (id, e) in edge.entries.iter_mut() { if *id == seven { e.step = 0; e.due = now - 10; } else { e.step = 1; e.due = now - 100; } }
        edge.ego_due = now + 1000; edge.session_due = now + 1000; edge.poses.clear();
        let nav = EgoRec::nav_byte(ego.nav_mode, ego.gnss, 2, false);
        let frame = edge.build_frame(&ego, nav, now, 100).expect("a frame");
        let ids: Vec<u16> = frame.records.iter().filter_map(|r| match r { Record::Contact(c) => Some(c.id), _ => None }).collect();
        assert_eq!(ids.len(), 3, "100 B frames take three 25 B contacts: {ids:?}");
        assert_eq!(ids[0], seven, "the never-sent newborn goes first: {ids:?}");
    }

    /// The operator focuses a group of three with split: each member comes back as its own child
    /// contact, every T_focus, while the group record falls back to its normal schedule.
    #[test]
    fn a_split_focus_sends_the_individuals_at_the_focus_rate() {
        let cfg = EdgeConfig { budget_bps: 2000, carrier_overhead: 0, ..Default::default() };
        let mut edge = Edge::new(cfg);
        let ego = EgoInput::default();
        let tracks: Vec<Track> = (1..=3).map(|i| Track { class: 0, ..track(i, i as f32 * 3.0) }).collect();
        let mut now = 0;
        while now <= 20 * TICK_HZ { edge.tick(&tracks, &ego, now); now += 12; }
        let gid = edge.cm.tracks[&1].contact.unwrap();
        assert_eq!(edge.cm.contacts[&gid].count(), 3);
        let focus = Frame { session: 0, seq: 0, tick: now, uplink: true, cycle_end: false,
            records: vec![Record::Focus(FocusRec { id: gid, mode: FOCUS_TRACK | FOCUS_SPLIT, ttl: 60, chip_px: 0 })] };
        edge.on_uplink(&focus.encode(false), now).unwrap();
        let mut sends: BTreeMap<u16, u32> = BTreeMap::new();
        let from = now + 5 * TICK_HZ;
        let mut rx = crate::receiver::Receiver::new(2000);
        while now <= from + 20 * TICK_HZ {
            // The receiver acks everything it holds every 5 s; focus must survive the acks.
            if now % (5 * TICK_HZ) == 0 && rx.session.is_some() { edge.on_uplink(&rx.make_digest(2000, 0, now), now).unwrap(); }
            for b in edge.tick(&tracks, &ego, now) {
                rx.on_frame(&b).unwrap();
                if now < from { continue; }
                for r in Frame::decode(&b).unwrap().records { if let Record::Contact(c) = r { *sends.entry(c.id).or_default() += 1; } }
            }
            now += 12;
        }
        let children: Vec<u16> = edge.cm.contacts.values().filter(|c| c.parent == Some(gid) && !c.departed).map(|c| c.id).collect();
        assert_eq!(children.len(), 3, "{:?}", edge.cm.contacts.values().map(|c| (c.id, c.parent, c.members.clone())).collect::<Vec<_>>());
        for id in &children { assert!(sends.get(id).copied().unwrap_or(0) >= 18, "child {id} sent {:?} times in 20 s", sends.get(id)); }
        assert!(sends.get(&gid).copied().unwrap_or(0) <= 4, "the group record is not repeated at 1 Hz: {sends:?}");

        // Drill down: the operator picks one child and releases the group. That child stays at the
        // focus rate; its siblings depart and fold back into the group.
        let pick = children[1];
        let up = |id: u16, mode: u8| Frame { session: 0, seq: 1, tick: now, uplink: true, cycle_end: false,
            records: vec![Record::Focus(FocusRec { id, mode, ttl: 60, chip_px: 0 })] }.encode(false);
        edge.on_uplink(&up(pick, FOCUS_TRACK), now).unwrap();
        edge.on_uplink(&up(gid, FOCUS_RELEASE), now).unwrap();
        let mut sends: BTreeMap<u16, u32> = BTreeMap::new();
        let from = now + 5 * TICK_HZ;
        while now <= from + 20 * TICK_HZ {
            for b in edge.tick(&tracks, &ego, now) {
                if now < from { continue; }
                for r in Frame::decode(&b).unwrap().records { if let Record::Contact(c) = r { if !c.has(F_DEPARTED) { *sends.entry(c.id).or_default() += 1; } } }
            }
            now += 12;
        }
        let live: Vec<u16> = edge.cm.contacts.values().filter(|c| c.is_child() && !c.departed).map(|c| c.id).collect();
        assert_eq!(live, vec![pick], "only the picked child is left");
        assert!(sends.get(&pick).copied().unwrap_or(0) >= 18, "picked child at 1 Hz: {sends:?}");
        assert!(!edge.cm.contacts[&gid].focused);
    }

    #[test]
    fn focused_records_take_at_most_half_the_link() {
        let mut edge = Edge::new(EdgeConfig { budget_bps: 600, ..Default::default() });
        let tracks: Vec<Track> = (1..=4).map(|i| Track { class: 0, ..track(i, i as f32 * 3.0) }).collect();
        let mut now = 0;
        while now <= 10 * TICK_HZ { edge.tick(&tracks, &EgoInput::default(), now); now += 12; }
        let base = edge.timing.focus;
        assert_eq!(edge.focus_period(), base, "nothing focused");
        let gid = edge.cm.tracks[&1].contact.unwrap();
        edge.cm.set_focus(gid, true, false);
        assert_eq!(edge.focus_period(), base, "one focused contact keeps T_focus at 600 bit/s");
        edge.cm.set_focus(gid, true, true);
        edge.tick(&tracks, &EgoInput::default(), now);
        let p = edge.focus_period() as f32 / TICK_HZ as f32;
        assert!(p > 3.0 && p < 5.0, "four children at 600 bit/s share half the link: {p} s");
    }
}
