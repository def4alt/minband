//! The edge: tracks and own state in, frames out. Owns the contact manager, the schedule of every
//! record, the pose queue, the token bucket for the budget, and the frame builder.

use crate::contacts::{Contact, ContactConfig, ContactManager, Detail, Track, DETAIL_LEVELS};
use crate::geo::{ce_m, ray_az_el};
use crate::scheduler::{target_frame_bytes, timing, Entry, Regime, Timing, LADDER_LEN};
use crate::wire::*;
use crate::TICK_HZ;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, VecDeque};

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
    /// A fixed detail level (`contacts::DETAIL`); None = the edge picks it from the load it measures.
    pub detail: Option<u8>,
    /// The radio paces the edge: the host reports what the link drained (`link_credit`) instead of
    /// the edge filling its token bucket from `budget_bps` (flow control, as RTS/CTS or a modem's
    /// free-buffer report). The budget then only sets the starting estimate and the timing.
    pub paced: bool,
    /// Backlog (seconds of link time) that coarsens the detail level (`COARSEN_S`). Lower keeps a
    /// busy picture fresher; higher keeps a quiet one precise (docs/PROTOCOL_EVAL.md 11).
    pub coarsen_s: f32,
}
impl Default for EdgeConfig {
    fn default() -> Self {
        EdgeConfig { device_id: 1, nonce: 1, origin_lat_e7: 0, origin_lon_e7: 0, origin_alt: 0x7FFF, pos_res: 2, caps: CAP_GNSS | CAP_BARO | CAP_MAG | CAP_IMU,
            utc_at_tick0: 0, hfov_x10: 850, img_w: 1920, img_h: 1080, video_frame0: 0xFFFF_FFFF, fps_x100: 0, budget_bps: 800, max_frame: 1200, crc: false,
            carrier_overhead: UDP_IP_OVERHEAD, sigma_own: 3.0, sigma_att_deg: 1.5, sigma_h: 2.0, sigma_px: 2.0, f_px: 2000.0, contacts: ContactConfig::default(),
            detail: None, paced: false, coarsen_s: COARSEN_S }
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
    /// Detail level, its vehicle link distance, and the load that set it (see `Load`).
    pub level: u8, pub link_m: f32, pub level_changes: u32,
    pub backlog_s: f32, pub backlog_b: u32, pub oldest_s: f32, pub drain_bps: f32, pub link_bps: f32,
}

/// Coarsen one level when the smoothed backlog stays above `COARSEN_S` seconds of link time (the
/// default of `EdgeConfig::coarsen_s`) for `COARSEN_FOR` and is not draining (a faster average,
/// `TREND_TAU_S`, down by `DRAINING` over that time: a burst of births, or the queue a change
/// leaves behind, is not a reason for a change). Refine one level when it stays below `REFINE_S`
/// for `REFINE_FOR`, the uplink is heard, and the finer level is predicted to fit within
/// `REFINE_HEADROOM` of what the link carries: the larger of first-copy demand scaled by the
/// contacts it would make and the change-threshold ratio, and the demand per contact last measured
/// at that level times its contacts; plus the births, `REC_B` each, drained within
/// `REFINE_BURST_S`. Hold `LEVEL_HOLD` after any change (longer than the split patience, so
/// regrouping settles). A refine undone by a coarsen within `REFINE_UNDONE` doubles the refine
/// wait, up to `REFINE_FOR_MAX`; one that holds halves it back (PROTOCOL.md 6.4).
pub const COARSEN_S: f32 = 2.0;
pub const COARSEN_FOR: u32 = 3 * TICK_HZ;
pub const DRAINING: f32 = 0.8;
pub const TREND_TAU_S: f32 = 0.5;
pub const REFINE_S: f32 = 0.3;
pub const REFINE_FOR: u32 = 15 * TICK_HZ;
pub const REFINE_FOR_MAX: u32 = 120 * TICK_HZ;
pub const REFINE_UNDONE: u32 = 30 * TICK_HZ;
pub const REFINE_HEADROOM: f32 = 0.5;
pub const REFINE_BURST_S: f32 = 10.0;
pub const REC_B: f32 = 25.0;
pub const LEVEL_HOLD: u32 = 10 * TICK_HZ;
/// What the link drained is counted over this window, first-copy demand over the longer one; the
/// backlog is smoothed with this time constant.
pub const DRAIN_WINDOW: u32 = 5 * TICK_HZ;
pub const DEMAND_WINDOW: u32 = 10 * TICK_HZ;
pub const BACKLOG_TAU_S: f32 = 2.0;

/// The starting level from the advertised budget: 0 in the video regime, 1 on wide and thin links,
/// 2 at the floor. A busy scene coarsens within seconds; a quiet one on a thin link is best at 1
/// (docs/PROTOCOL_EVAL.md 11).
pub fn seed_level(budget_bps: u32) -> u8 {
    match timing(budget_bps).regime { Regime::Video => 0, Regime::Wide | Regime::Thin => 1, Regime::Floor => 2 }
}

/// The finest level the controller refines to: 0 only on links of 8 kbit/s or more (video and wide
/// regimes); on thinner ones its extra revisions cost more than its tighter groups give.
pub fn finest_level(link_bps: u32) -> u8 {
    match timing(link_bps).regime { Regime::Video | Regime::Wide => 0, _ => 1 }
}

/// The load signal and the level controller. Backlog = bytes of contact records due on the ladder
/// (first copies and repeats) and not sent after the frame builder ran (focused records and merge
/// tombstones excluded), over what the link drained in the last `DRAIN_WINDOW`: seconds of link
/// time. It does not depend on the budget being right.
#[derive(Clone, Debug, Default, Serialize)]
pub struct Load {
    pub level: u8,
    pub fixed: bool,
    pub coarsen_s: f32,
    pub backlog_b: u32,
    pub oldest_s: f32,
    pub drain_bps: f32,
    pub backlog_s: f32,
    pub smooth_s: f32,
    pub fast_s: f32,
    /// First copies of revisions entering the queue, bit/s, and what the last refine check predicted.
    pub demand_bps: f32,
    pub predicted_bps: f32,
    pub changes: u32,
    pub changed_at: Option<u32>,
    pub refine_for: u32,
    /// First-copy demand per reported contact (bit/s) last measured while each level was held.
    pub per_contact: [Option<f32>; 5],
    probe: Option<u32>,
    above_since: Option<u32>,
    below_since: Option<u32>,
    #[serde(skip)] sent: VecDeque<(u32, u32)>,
    #[serde(skip)] credits: VecDeque<(u32, u32)>,
    #[serde(skip)] demand: VecDeque<(u32, u32)>,
    #[serde(skip)] trend: VecDeque<(u32, f32)>,
    first: Option<u32>,
    last: Option<u32>,
}

/// What the edge knows about the link at one step.
pub struct LinkView<'a> {
    pub backlog_b: u32,
    pub oldest: u32,
    pub hears: bool,
    /// Application bit/s the link carries (budget or radio credits); None = unlimited.
    pub capacity_bps: Option<f32>,
    /// The finest level allowed on this link (`finest_level`).
    pub finest: u8,
    /// Reported top-level contacts now.
    pub contacts: usize,
    /// For a refine: contacts the tracks make at this level and at the next finer one, and the ratio
    /// of their change thresholds.
    pub finer: &'a dyn Fn() -> (usize, usize, f32),
}

impl Load {
    fn new(level: u8, fixed: bool) -> Self { Load { level, fixed, coarsen_s: COARSEN_S, refine_for: REFINE_FOR, ..Default::default() } }
    fn window(q: &mut VecDeque<(u32, u32)>, now: u32, w: u32) -> u32 {
        while q.front().map_or(false, |&(t, _)| now.saturating_sub(t) >= w) { q.pop_front(); }
        q.iter().map(|&(_, b)| b).sum()
    }
    fn span_s(&self, now: u32, w: u32) -> f32 { (now.saturating_sub(self.first.unwrap_or(now)).min(w).max(TICK_HZ)) as f32 / TICK_HZ as f32 }
    /// Bit/s the radio's credits allowed (paced mode), if any.
    fn credit_bps(&mut self, now: u32) -> Option<f32> {
        if self.credits.is_empty() { return None; }
        let b = Self::window(&mut self.credits, now, DRAIN_WINDOW);
        Some(b as f32 * 8.0 / self.span_s(now, DRAIN_WINDOW))
    }
    /// One measurement after the frame builder ran; returns the new level when it changes.
    fn step(&mut self, m: LinkView, now: u32) -> Option<u8> {
        self.first.get_or_insert(now);
        let dt = self.last.map_or(0, |l| now.saturating_sub(l)) as f32 / TICK_HZ as f32;
        self.last = Some(now);
        let drained = Self::window(&mut self.sent, now, DRAIN_WINDOW) as f32 / self.span_s(now, DRAIN_WINDOW);
        self.drain_bps = drained * 8.0;
        self.demand_bps = Self::window(&mut self.demand, now, DEMAND_WINDOW) as f32 * 8.0 / self.span_s(now, DEMAND_WINDOW);
        self.backlog_b = m.backlog_b; self.oldest_s = m.oldest as f32 / TICK_HZ as f32;
        self.backlog_s = if m.backlog_b == 0 { 0.0 } else if drained <= 0.0 { 60.0 } else { (m.backlog_b as f32 / drained).min(60.0) };
        self.smooth_s += (self.backlog_s - self.smooth_s) * (1.0 - (-dt / BACKLOG_TAU_S).exp());
        self.fast_s += (self.backlog_s - self.fast_s) * (1.0 - (-dt / TREND_TAU_S).exp());
        self.trend.push_back((now, self.fast_s));
        while self.trend.len() > 1 && now.saturating_sub(self.trend[1].0) >= COARSEN_FOR { self.trend.pop_front(); }
        if let Some(p) = self.probe { if now.saturating_sub(p) > REFINE_UNDONE { self.probe = None; self.refine_for = (self.refine_for / 2).max(REFINE_FOR); } }
        // Once the demand window holds this level only, remember what a contact costs at it.
        if now.saturating_sub(self.changed_at.or(self.first).unwrap_or(now)) >= DEMAND_WINDOW {
            let r = self.demand_bps / m.contacts.max(1) as f32;
            let pc = &mut self.per_contact[self.level as usize];
            *pc = Some(pc.map_or(r, |x| x + (r - x) * 0.05));
        }
        if self.fixed { return None; }
        if self.smooth_s > self.coarsen_s { self.above_since.get_or_insert(now); } else { self.above_since = None; }
        // A silent uplink means jamming or trouble: never refine on it.
        if self.smooth_s < REFINE_S && m.hears { self.below_since.get_or_insert(now); } else { self.below_since = None; }
        if self.changed_at.map_or(false, |t| now.saturating_sub(t) < LEVEL_HOLD) { return None; }
        let held = |s: Option<u32>, d: u32| s.map_or(false, |t| now.saturating_sub(t) >= d);
        let draining = self.trend.front().map_or(false, |&(t, v)| now.saturating_sub(t) >= COARSEN_FOR && self.fast_s < DRAINING * v);
        let next = if self.level + 1 < DETAIL_LEVELS && held(self.above_since, COARSEN_FOR) && !draining {
            if self.probe.take().is_some() { self.refine_for = (self.refine_for * 2).min(REFINE_FOR_MAX); }
            self.level + 1
        } else if self.level > m.finest && held(self.below_since, self.refine_for) {
            let (n_now, n_fine, dev) = (m.finer)();
            let scaled = self.demand_bps * n_fine as f32 / n_now.max(1) as f32 * dev;
            let known = self.per_contact[self.level as usize - 1].map_or(0.0, |r| r * n_fine as f32);
            let births = n_fine.saturating_sub(n_now) as f32 * REC_B * 8.0 / REFINE_BURST_S;
            self.predicted_bps = scaled.max(known) + births;
            if m.capacity_bps.map_or(false, |c| self.predicted_bps > REFINE_HEADROOM * c) { return None; }
            self.probe = Some(now);
            self.level - 1
        } else { return None };
        self.level = next; self.changes += 1; self.changed_at = Some(now);
        self.above_since = None; self.below_since = None;
        Some(next)
    }
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
    pub load: Load,
}

impl Edge {
    pub fn new(cfg: EdgeConfig) -> Self {
        let t = timing(cfg.budget_bps);
        let mut cm = ContactManager::new(cfg.contacts, pos_res_m(cfg.pos_res));
        let level = cfg.detail.unwrap_or_else(|| seed_level(cfg.budget_bps)).min(DETAIL_LEVELS - 1);
        cm.set_level(level);
        Edge { timing: t, cm, entries: BTreeMap::new(), ego_due: 0, last_nav: None, session_due: 0, session_sends: 0, poses: Vec::new(), last_pose_tick: None,
            focus: BTreeMap::new(), tokens: 0.0, last_tick: None, last_frame_tick: None, stats: EdgeStats { budget_bps: cfg.budget_bps, regime: Some(t.regime), ..Default::default() },
            last_ego: None, uplink_heard: None, load: Load { coarsen_s: cfg.coarsen_s, ..Load::new(level, cfg.detail.is_some()) }, cfg }
    }

    /// Paced mode: the radio drained `bytes` (or has that much more room) since the last call.
    pub fn link_credit(&mut self, bytes: u32, now: u32) {
        self.tokens += bytes as f32;
        self.load.credits.push_back((now, bytes));
    }

    /// The link's rate as the edge knows it: measured from the radio's credits when paced, else
    /// the budget (0 = unlimited).
    fn link_bps(&mut self, now: u32) -> u32 {
        if self.cfg.paced { if let Some(b) = self.load.credit_bps(now) { return (b.round() as u32).max(1); } }
        self.cfg.budget_bps
    }
    fn frame_target(&mut self, now: u32) -> usize { target_frame_bytes(self.link_bps(now), self.cfg.max_frame) }

    /// Moves to a detail level (fixed, or from the controller).
    pub fn set_level(&mut self, n: u8, now: u32) {
        let n = n.min(DETAIL_LEVELS - 1);
        if n == self.cm.level { return; }
        if self.load.level != n { self.load.level = n; self.load.changes += 1; self.load.changed_at = Some(now); }
        self.cm.set_level(n);
    }
    pub fn detail(&self) -> Detail { self.cm.detail() }

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
        // cdeg in an i16 holds +-327.67 deg: angles go on the wire in -180..180 (a bearing of 350 deg is -10).
        let wrap = |d: f32| (d + 180.0).rem_euclid(360.0) - 180.0;
        let cd = |d: f32| (wrap(d) * 100.0).round().clamp(-18000.0, 17999.0) as i16;
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
            let (_, el) = ray_az_el(ego.e, ego.n, ego.alt_agl, t.e, t.n, t.u.unwrap_or(0.0));
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
        let gap = self.focus_share_gap(now);
        for id in changed {
            let Some(c) = self.cm.contacts.get(&id) else { continue };
            if !self.cm.reportable(c) { continue; }
            if !c.fast() && !self.merge_tombstone(c) { self.load.demand.push_back((now, Record::Contact(self.contact_rec(c, ego, now)).wire_len() as u32)); }
            let fast = c.fast() && !c.departed && !c.lost;
            let e = self.entries.entry(id).or_insert_with(Entry::default);
            match (fast, e.last_sent) {
                (true, Some(last)) => { e.step = 0; e.due = e.due.min(now.max(last + gap)); }
                _ => e.changed(now),
            }
        }
        // Below the detail level's report-age gate a contact has no schedule entry and costs nothing;
        // one that departs before it was ever reported just goes.
        let gate: Vec<(u16, bool, bool)> = self.cm.contacts.values().map(|c| (c.id, self.cm.reportable(c), c.departed)).collect();
        for (id, ok, departed) in gate {
            if ok { self.entries.entry(id).or_insert_with(|| { let mut e = Entry::default(); e.changed(now); e }); }
            else { self.entries.remove(&id); if departed { self.cm.remove(id); } }
        }
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
        let target = self.frame_target(now);
        if self.cfg.paced { self.tokens = self.tokens.min(2.0 * target as f32); }
        else if self.cfg.budget_bps > 0 {
            self.tokens = (self.tokens + dt * self.cfg.budget_bps as f32 / 8.0).min(2.0 * target as f32);
        }
        let mut out = Vec::new();
        loop {
            let unlimited = self.cfg.budget_bps == 0 && !self.cfg.paced;
            if unlimited { if self.last_frame_tick.map_or(false, |l| now.saturating_sub(l) < TICK_HZ / 10) { break; } }
            else if self.tokens < target as f32 { break; }
            let Some(frame) = self.build_frame(ego, nav, now, target) else { break };
            let bytes = frame.encode(self.cfg.crc);
            self.tokens -= (bytes.len() + self.cfg.carrier_overhead) as f32;
            self.stats.frames += 1; self.stats.bytes += bytes.len() as u64; self.stats.bytes_wire += (bytes.len() + self.cfg.carrier_overhead) as u64;
            self.last_frame_tick = Some(now);
            self.load.sent.push_back((now, bytes.len() as u32));
            out.push(bytes);
            if unlimited { break; }
        }
        self.stats.tokens = self.tokens;
        // The load after this tick's frames, and the level it asks for.
        let (backlog_b, oldest) = self.backlog(ego, now);
        let hears = self.link_state(now) == 0;
        let link_bps = self.link_bps(now) as f32;
        let capacity_bps = if link_bps <= 0.0 { None } else {
            let target = self.frame_target(now) as f32;
            Some(link_bps * target / (target + self.cfg.carrier_overhead as f32))
        };
        let cm = &self.cm;
        let contacts = cm.contacts.values().filter(|c| !c.departed && !c.is_child() && cm.reportable(c)).count();
        let finer = || {
            if cm.level == 0 { return (1, 1, 1.0); }
            (cm.clusters_at(cm.level, now), cm.clusters_at(cm.level - 1, now), cm.ladder(cm.level).dev_factor / cm.ladder(cm.level - 1).dev_factor)
        };
        let finest = finest_level(link_bps as u32);
        if let Some(n) = self.load.step(LinkView { backlog_b, oldest, hears, capacity_bps, finest, contacts, finer: &finer }, now) { self.cm.set_level(n); }
        let (l, st) = (&self.load, &mut self.stats);
        st.level = self.cm.level; st.link_m = self.cm.cfg.link_m; st.level_changes = l.changes;
        st.backlog_s = l.smooth_s; st.backlog_b = l.backlog_b; st.oldest_s = l.oldest_s; st.drain_bps = l.drain_bps; st.link_bps = link_bps;
        out
    }

    /// Bytes of ladder records (first copies and repeats) due now, and how long the oldest has
    /// waited. Floor repeats are housekeeping; focused records have their own share; merge
    /// tombstones ride at the floor class and would otherwise make one coarsening ask for the next.
    fn backlog(&self, ego: &EgoInput, now: u32) -> (u32, u32) {
        let (mut b, mut oldest) = (0u32, 0u32);
        for (id, e) in &self.entries {
            if e.due > now || e.step >= LADDER_LEN { continue; }
            let Some(c) = self.cm.contacts.get(id) else { continue };
            if c.fast() || self.merge_tombstone(c) { continue; }
            b += Record::Contact(self.contact_rec(c, ego, now)).wire_len() as u32;
            oldest = oldest.max(now - e.due);
        }
        (b, oldest)
    }

    /// A tombstone of a contact absorbed by a merge within `LEVEL_HOLD` + the split patience of a
    /// level change: the merged group already carries its members.
    fn merge_tombstone(&self, c: &Contact) -> bool {
        c.departed && c.absorbed && match (self.load.changed_at, c.departed_at) {
            (Some(t), Some(d)) => d >= t && d - t <= LEVEL_HOLD + self.cm.cfg.t_split,
            _ => false,
        }
    }

    fn link_state(&self, now: u32) -> u8 {
        match self.uplink_heard { None => 2, Some(t) if now.saturating_sub(t) > 30 * TICK_HZ => 1, _ => 0 }
    }


    /// T_focus for this frame. Focused records take at most `FOCUS_SHARE` of the link: when more
    /// are focused (a split group of four, say) the period stretches so the rest of the picture keeps
    /// the other half. One focused contact never stretches it on the thin profiles (PROTOCOL.md 6.2).
    fn focus_period(&mut self, now: u32) -> u32 { self.timing.focus.max(self.focus_share_gap(now)) }

    /// The shortest gap between two sends of one focused record that keeps all focused records
    /// within `FOCUS_SHARE` of the link (0 on an unlimited link).
    fn focus_share_gap(&mut self, now: u32) -> u32 {
        let bps = self.link_bps(now);
        if bps == 0 { return 0; }
        let n = self.cm.contacts.values().filter(|c| c.fast() && !c.departed).count() as f32;
        if n == 0.0 { return 0; }
        let target = target_frame_bytes(bps, self.cfg.max_frame) as f32;
        let app_bytes_per_s = bps as f32 / 8.0 * target / (target + self.cfg.carrier_overhead as f32);
        let gap_s = n * FOCUS_RECORD_B / (FOCUS_SHARE * app_bytes_per_s);
        (gap_s * TICK_HZ as f32).ceil() as u32
    }

    fn build_frame(&mut self, ego: &EgoInput, nav: u8, now: u32, target: usize) -> Option<Frame> {
        let mut t = self.timing;
        t.focus = self.focus_period(now);
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
            let rank = if c.fast() { 0 } else if self.merge_tombstone(c) { 6 } else if e.step < LADDER_LEN { 2 } else if c.departed { 6 } else { 5 };
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
        let mut sent_recs: Vec<ContactRec> = Vec::new();
        let mut sent_ego = false; let mut sent_session = false;
        for (_, _, _, k) in cands {
            match k {
                K::Contact(id) => {
                    let c = &self.cm.contacts[&id];
                    let rec = self.contact_rec(c, ego, now);
                    let n = Record::Contact(rec).wire_len();
                    if len + n > room && !records.is_empty() { continue; }
                    if len + n > self.cfg.max_frame { continue; }
                    records.push(Record::Contact(rec)); len += n; sent_ids.push(id); sent_recs.push(rec);
                }
                K::Ego => {
                    let rec = self.ego_rec(ego, nav, now);
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
        for (id, rec) in sent_ids.iter().zip(&sent_recs) {
            let fast = self.cm.contacts[id].fast();
            self.entries.get_mut(id).unwrap().sent(now, &t, fast);
            self.cm.mark_sent(*id, now);
            let observed = now.saturating_sub(rec.age as u32 * TICK_HZ);
            if let Some(c) = self.cm.contacts.get_mut(id) { c.sent_pred = Some(crate::geo::SentPred::from_rec(rec, self.cfg.pos_res, observed)); }
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

    /// Seconds the oldest never-sent revision has waited (Ego.backlog): how late fresh news is.
    fn backlog_s(&self, now: u32) -> u8 {
        let w = self.entries.values().filter(|e| e.step == 0 && e.due <= now).map(|e| now - e.due).max().unwrap_or(0);
        ((w + TICK_HZ - 1) / TICK_HZ).min(255) as u8
    }

    fn ego_rec(&mut self, ego: &EgoInput, nav: u8, now: u32) -> EgoRec {
        let (n, moving, mix) = self.cm.summary();
        let r = self.cfg.pos_res;
        let rec = EgoRec { dx: m_to_pos(ego.e, r), dy: m_to_pos(ego.n, r), alt_agl: if ego.alt_agl.is_finite() { ego.alt_agl.round().clamp(-32768.0, 32766.0) as i16 } else { 0x7FFF },
            heading: deg_to_u8(ego.heading_deg), speed: speed_to_u8(ego.speed), climb: climb_to_i8(ego.climb), nav, battery: ego.battery, pos_ce: m_to_m8(ego.pos_ce),
            fp_dx: m_to_pos(ego.fp_e, r), fp_dy: m_to_pos(ego.fp_n, r), fp_radius: m_to_m8(ego.fp_radius),
            n_contacts: n, n_moving: moving, n_dismount: mix[0], n_vehicle: mix[1], n_armour: mix[2], n_other: mix[3], backlog: self.backlog_s(now), group_m: m_to_m8(self.cm.cfg.link_m) };
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
        // A height from the tracker (a 3D reconstruction, a rangefinder) goes on the wire as `dz`, and
        // the ray points at the object itself instead of at the flat ground below it.
        let dz = c.u.filter(|u| u.is_finite() && !c.departed).map(|u| u.round().clamp(-32768.0, 32767.0) as i16);
        if dz.is_some() { ext |= X_ALT; }
        let (az, el) = ray_az_el(ego.e, ego.n, ego.alt_agl, c.e, c.n, c.u.unwrap_or(0.0));
        let bb = c.bbox.unwrap_or([0.0; 4]);
        ContactRec { id: c.id, rev: c.rev, flags, ext, dx: m_to_pos(c.e, r), dy: m_to_pos(c.n, r), ce: m_to_m8(self.cm.declared_ce(c)), radius: m_to_m8(c.radius),
            n_dismount: c.mix[0], n_vehicle: c.mix[1], n_armour: c.mix[2], n_other: c.mix[3], conf: c.conf,
            first_seen: secs_u16(c.first_seen), since: secs_u16(c.since), age: age_u8(now.saturating_sub(c.last_seen)),
            course: deg_to_u8(c.course), speed: speed_to_u8(c.speed), parent: c.parent.unwrap_or(0), dz: dz.unwrap_or(0),
            az: deg_to_u8(az), el: el_to_u8(el), bbox: [nrm_to_u8(bb[0]), nrm_to_u8(bb[1]), nrm_to_u8(bb[2]), nrm_to_u8(bb[3])] }
    }

    /// The edge's own view, for the side-by-side page: contacts as it holds them.
    pub fn snapshot(&self, now: u32) -> EdgeSnapshot {
        let contacts = self.cm.contacts.values().map(|c| ContactView::from_contact(c, now, self.entries.get(&c.id))).collect();
        EdgeSnapshot { contacts, tracks: self.cm.tracks.values().map(|t| TrackView { id: t.id, class: t.class, e: t.e, n: t.n, u: t.u, ve: t.ve, vn: t.vn, conf: t.conf, ce: t.ce, lost: t.lost, contact: t.contact, motion: motion_name(t.motion) }).collect(),
            timing: self.timing, tokens: self.tokens, focus: self.focus.keys().copied().collect(), stats: self.cm.stats.clone(),
            detail: DetailView { level: self.cm.level, detail: self.cm.detail(), link_m: self.cm.cfg.link_m, link_dismount_m: self.cm.cfg.link_dismount_m, dev_factor: self.cm.cfg.dev_factor, load: self.load.clone(), ladder: self.cm.cfg.ladder } }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct TrackView { pub id: u32, pub class: u8, pub e: f32, pub n: f32, pub u: Option<f32>, pub ve: f32, pub vn: f32, pub conf: u8, pub ce: f32, pub lost: bool, pub contact: Option<u16>, pub motion: &'static str }

#[derive(Clone, Debug, Serialize)]
pub struct ContactView {
    pub id: u16, pub rev: u8, pub e: f32, pub n: f32, pub u: Option<f32>, pub ce: f32, pub radius: f32, pub count: u32, pub mix: [u8; 4],
    pub motion: &'static str, pub confirmed: bool, pub lost: bool, pub departed: bool, pub focused: bool, pub split: bool,
    pub course: f32, pub speed: f32, pub members: Vec<u32>, pub first_seen: f32, pub since: f32, pub parent: Option<u16>,
    pub dirty: bool, pub step: u8, pub due_in: f32, pub sends: u32, pub bbox: Option<[f32; 4]>,
    /// The edge's estimate carried to `now`: `e, n` were measured `silent_s` ago; a moving contact
    /// has moved on at its velocity since (what the edge believes now, for evaluation).
    pub now_e: f32, pub now_n: f32, pub silent_s: f32,
}
impl ContactView {
    pub fn from_contact(c: &Contact, now: u32, e: Option<&Entry>) -> Self {
        let s = |t: u32| t as f32 / TICK_HZ as f32;
        ContactView { id: c.id, rev: c.rev, e: c.e, n: c.n, u: c.u, ce: c.ce, radius: c.radius, count: c.count(), mix: c.mix, motion: motion_name(c.motion), confirmed: c.confirmed,
            lost: c.lost, departed: c.departed, focused: c.focused, split: c.split, course: c.course, speed: c.speed, members: c.members.clone(), first_seen: s(c.first_seen),
            since: s(c.since), parent: c.parent, dirty: c.dirty, step: e.map_or(0, |e| e.step), due_in: e.map_or(0.0, |e| (e.due as i64 - now as i64) as f32 / TICK_HZ as f32), sends: e.map_or(0, |e| e.sends), bbox: c.bbox,
            now_e: c.e + if c.motion == MOTION_MOVING { c.ve * s(now.saturating_sub(c.last_seen)) } else { 0.0 },
            now_n: c.n + if c.motion == MOTION_MOVING { c.vn * s(now.saturating_sub(c.last_seen)) } else { 0.0 },
            silent_s: s(now.saturating_sub(c.last_seen)) }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct EdgeSnapshot { pub contacts: Vec<ContactView>, pub tracks: Vec<TrackView>, pub timing: Timing, pub tokens: f32, pub focus: Vec<u16>, pub stats: crate::contacts::ContactStats, pub detail: DetailView }

/// The detail level as applied (link distances and threshold from the configured ones) and the load.
#[derive(Clone, Debug, Serialize)]
pub struct DetailView { pub level: u8, pub detail: Detail, pub link_m: f32, pub link_dismount_m: f32, pub dev_factor: f32, pub load: Load, pub ladder: [Detail; 5] }

/// Share of the link focused records may take, and the size assumed for one (a moving child with
/// its ray, framing included).
pub const FOCUS_SHARE: f32 = 0.5;
pub const FOCUS_RECORD_B: f32 = 30.0;

#[cfg(test)]
mod tests {
    use super::*;

    fn track(id: u32, e: f32) -> Track { Track { id, class: 2, e, n: 0.0, ve: 0.0, vn: 0.0, conf: 200, ce: Some(3.0), bbox: None, u: None } }

    /// A pose's yaw is a bearing (0..360) but its cdeg field is an i16: 359.3 deg must arrive as -0.7, not
    /// saturate at 327.67.
    #[test]
    fn a_pose_bearing_past_327_degrees_wraps() {
        let cfg = EdgeConfig { budget_bps: 0, carrier_overhead: 0, ..Default::default() };
        let mut edge = Edge::new(cfg);
        edge.pose(0, 1.0, 2.0, 30.0, 359.3, -15.0, 350.0);
        let p = edge.poses.last().copied().expect("a pose in the video regime");
        assert_eq!((p.yaw, p.pitch, p.roll), (-70, -1500, -1000));
    }

    /// A track placed in 3D (tools/recon3d: a reconstruction's terrain) reaches the receiver with its
    /// height in `dz`, and its ray points at the object, not at the flat ground under it; a track
    /// without a height sends none.
    #[test]
    fn a_height_from_the_tracker_goes_on_the_wire() {
        let cfg = EdgeConfig { budget_bps: 0, carrier_overhead: 0, ..Default::default() };
        let mut edge = Edge::new(cfg);
        let ego = EgoInput { alt_agl: 40.0, ..Default::default() };
        let tracks = vec![Track { u: Some(12.4), ..track(1, 30.0) }, track(2, 300.0)];
        let mut rx = crate::receiver::Receiver::new(0);
        let mut sent = Vec::new();
        let mut now = 0;
        while now <= 5 * TICK_HZ {
            for b in edge.tick(&tracks, &ego, now) {
                for r in Frame::decode(&b).unwrap().records { if let Record::Contact(c) = r { sent.push(c); } }
                rx.on_frame(&b).unwrap();
            }
            now += 12;
        }
        let c1 = edge.cm.tracks[&1].contact.unwrap();
        let c2 = edge.cm.tracks[&2].contact.unwrap();
        let rec1 = sent.iter().rev().find(|c| c.id == c1).expect("contact 1 sent");
        let rec2 = sent.iter().rev().find(|c| c.id == c2).expect("contact 2 sent");
        assert!(rec1.ext_has(X_ALT) && rec1.dz == 12, "dz on the wire: {rec1:?}");
        assert!(!rec2.ext_has(X_ALT), "no height, no dz: {rec2:?}");
        // The ray to a point 12.4 m up from 40 m, 30 m away: depression atan(27.6 / 30), not atan(40 / 30).
        let el = u8_to_el(rec1.el);
        assert!((el - (27.6f32 / 30.0).atan().to_degrees()).abs() < 0.5, "ray at the object: {el}");
        let held = rx.snapshot(now);
        assert_eq!(held.iter().find(|c| c.id == c1).unwrap().u, Some(12.0));
        assert_eq!(held.iter().find(|c| c.id == c2).unwrap().u, None);
    }
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

    /// Forty people walking a zig-zag together, 20 m apart (individuals at levels 0-2, one file of
    /// groups at 3), every one revising on each turn.
    fn crowd(t: u32) -> Vec<Track> {
        let s = t as f32 / TICK_HZ as f32;
        let leg = (s / 4.0) as u32; let along = s - leg as f32 * 4.0;
        let (ve, vn) = if leg % 2 == 0 { (1.2, 0.6) } else { (1.2, -0.6) };
        let (e0, n0) = (1.2 * 4.0 * leg as f32, 0.0);
        (0..40).map(|i| Track { id: i + 1, class: 0, e: e0 + ve * along + 20.0 * (i % 8) as f32, n: n0 + vn * along + 20.0 * (i / 8) as f32, ve, vn, conf: 200, ce: Some(3.0), bbox: None, u: None }).collect()
    }
    fn digest(edge: &mut Edge, rx: &mut crate::receiver::Receiver, now: u32) {
        if rx.session.is_some() { let d = rx.make_digest(edge.cfg.budget_bps, 0, now); edge.on_uplink(&d, now).unwrap(); }
    }

    #[test]
    fn a_busy_scene_on_a_thin_link_coarsens() {
        let mut edge = Edge::new(EdgeConfig { budget_bps: 600, ..Default::default() });
        assert_eq!(edge.cm.level, 1, "600 bit/s starts at level 1");
        let mut rx = crate::receiver::Receiver::new(600);
        let mut now = 0; let mut levels = Vec::new();
        while now <= 90 * TICK_HZ {
            for b in edge.tick(&crowd(now), &EgoInput::default(), now) { rx.on_frame(&b).unwrap(); }
            if now % (5 * TICK_HZ) == 0 { digest(&mut edge, &mut rx, now); }
            if now % TICK_HZ == 0 { levels.push((now / TICK_HZ, edge.cm.level, edge.load.smooth_s)); }
            now += 12;
        }
        assert!(edge.cm.level >= 3, "coarsened: {levels:?}");
        let live = edge.cm.contacts.values().filter(|c| !c.departed && !c.is_child()).count();
        assert!(live <= 10, "forty walkers in at most ten contacts at level {}: {live}", edge.cm.level);
        assert!(edge.load.changes <= 2, "the merge tombstones do not ask for a second coarsening: {levels:?}");
        assert!(edge.load.smooth_s < COARSEN_S, "the backlog drained: {levels:?}");
    }

    #[test]
    fn a_quiet_scene_refines_only_while_the_uplink_is_heard() {
        let cars = |_t: u32| (1..=3).map(|i| track(i, i as f32 * 100.0)).collect::<Vec<_>>();
        for heard in [false, true] {
            let mut edge = Edge::new(EdgeConfig { budget_bps: 2000, ..Default::default() });
            edge.set_level(3, 0);
            let mut rx = crate::receiver::Receiver::new(2000);
            let mut now = 0;
            while now <= 120 * TICK_HZ {
                for b in edge.tick(&cars(now), &EgoInput::default(), now) { rx.on_frame(&b).unwrap(); }
                if heard && now % (5 * TICK_HZ) == 0 { digest(&mut edge, &mut rx, now); }
                now += 12;
            }
            if heard { assert_eq!(edge.cm.level, 1, "refined to the finest level a thin link allows"); assert_eq!(edge.load.changes, 3); }
            else { assert_eq!(edge.cm.level, 3, "never refines on a silent uplink"); }
        }
    }

    #[test]
    fn the_level_holds_after_a_change_and_refines_only_what_fits() {
        let one = || (1usize, 1usize, 1.0f32);
        let view = |b: u32| LinkView { backlog_b: b, oldest: 0, hears: true, capacity_bps: Some(1000.0), finest: 0, contacts: 1, finer: &one };
        let mut l = Load::new(1, false);
        // Saturated: 2000 B due, 100 B/s drained: one level per hold, never faster.
        let (mut now, mut changes) = (0u32, Vec::new());
        while now <= 60 * TICK_HZ {
            l.sent.push_back((now, 10));
            if let Some(n) = l.step(view(2000), now) { changes.push((now, n)); }
            now += 12;
        }
        assert_eq!(changes.iter().map(|c| c.1).collect::<Vec<_>>(), vec![2, 3, 4]);
        for w in changes.windows(2) { assert!(w[1].0 - w[0].0 >= LEVEL_HOLD, "{changes:?}"); }
        // Quiet: one refine after REFINE_FOR (plus the backlog's decay); the load comes back at once,
        // the refine is undone, and the next one waits twice as long.
        // Steps until the first change (or `secs`).
        let run = |l: &mut Load, from: &mut u32, secs: u32, backlog: u32| -> Vec<(u32, u8)> {
            let end = *from + secs * TICK_HZ;
            while *from <= end { l.sent.push_back((*from, 10)); let r = l.step(view(backlog), *from); *from += 12; if let Some(n) = r { return vec![(*from - 12, n)]; } }
            vec![]
        };
        let t0 = now;
        let c = run(&mut l, &mut now, 60, 0);
        assert_eq!(c[0].1, 3); assert!(c[0].0 - t0 >= REFINE_FOR, "{c:?}");
        let c = run(&mut l, &mut now, 15, 2000);
        assert_eq!(c.iter().map(|c| c.1).collect::<Vec<_>>(), vec![4], "undone after the hold: {c:?}");
        assert_eq!(l.refine_for, 2 * REFINE_FOR);
        let t2 = now;
        let c = run(&mut l, &mut now, 120, 0);
        assert!(c[0].0 - t2 >= 2 * REFINE_FOR, "the second refine waited longer: {c:?}");
        run(&mut l, &mut now, REFINE_UNDONE / TICK_HZ + 1, 0);
        assert_eq!(l.refine_for, REFINE_FOR, "a refine that holds halves the wait back");
        // A finer level that would not fit is not tried: demand 4 kbit/s on a 1 kbit/s link.
        let mut l = Load::new(3, false);
        let mut now = 0;
        while now <= 120 * TICK_HZ {
            l.sent.push_back((now, 10)); l.demand.push_back((now, 50));
            assert_eq!(l.step(view(0), now), None, "refined into a level that cannot fit");
            now += 12;
        }
        assert!(l.predicted_bps > 3000.0);
    }

    #[test]
    fn focus_stays_individual_at_level_4() {
        let mut edge = Edge::new(EdgeConfig { budget_bps: 2000, carrier_overhead: 0, detail: Some(4), ..Default::default() });
        let ego = EgoInput::default();
        // Car 1 parked; car 2 parked 200 m away, then 50 m away (one group at level 4).
        let scene = |near: bool| move |_t: u32| vec![track(1, 0.0), track(2, if near { 50.0 } else { 200.0 })];
        let mut now = 0;
        while now <= 12 * TICK_HZ { edge.tick(&scene(false)(now), &ego, now); now += 12; }
        let c1 = edge.cm.tracks[&1].contact.unwrap();
        let up = |id: u16, mode: u8, now: u32| Frame { session: 0, seq: 0, tick: now, uplink: true, cycle_end: false,
            records: vec![Record::Focus(FocusRec { id, mode, ttl: 60, chip_px: 0 })] }.encode(false);
        edge.on_uplink(&up(c1, FOCUS_TRACK, now), now).unwrap();
        let mut sends = 0;
        let from = now;
        while now <= from + 20 * TICK_HZ {
            for b in edge.tick(&scene(true)(now), &ego, now) {
                for r in Frame::decode(&b).unwrap().records { if let Record::Contact(c) = r { if c.id == c1 { sends += 1; } } }
            }
            now += 12;
        }
        assert_eq!(edge.cm.level, 4);
        assert_eq!(edge.cm.contacts[&c1].members, vec![1], "the focused car is not absorbed at 120 m");
        assert!(sends >= 18, "on the focus schedule: {sends} in 20 s");
        // A split group at level 4: three people 40 m apart are one group, each child goes out.
        let mut edge = Edge::new(EdgeConfig { budget_bps: 2000, carrier_overhead: 0, detail: Some(4), ..Default::default() });
        let people: Vec<Track> = (1..=3).map(|i| Track { class: 0, ..track(i, i as f32 * 40.0) }).collect();
        let mut now = 0;
        while now <= 15 * TICK_HZ { edge.tick(&people, &ego, now); now += 12; }
        let gid = edge.cm.tracks[&1].contact.unwrap();
        assert_eq!(edge.cm.contacts[&gid].count(), 3);
        edge.on_uplink(&up(gid, FOCUS_TRACK | FOCUS_SPLIT, now), now).unwrap();
        let mut sends: BTreeMap<u16, u32> = BTreeMap::new();
        let from = now + 2 * TICK_HZ;
        while now <= from + 20 * TICK_HZ {
            for b in edge.tick(&people, &ego, now) {
                if now < from { continue; }
                for r in Frame::decode(&b).unwrap().records { if let Record::Contact(c) = r { if c.has(F_FOCUSED) && c.parent == gid { *sends.entry(c.id).or_default() += 1; } } }
            }
            now += 12;
        }
        assert_eq!(sends.len(), 3, "{sends:?}");
        assert!(sends.values().all(|&n| n >= 18), "children at the focus rate: {sends:?}");
    }

    #[test]
    fn a_ghost_below_the_age_gate_costs_nothing() {
        let mut edge = Edge::new(EdgeConfig { budget_bps: 0, detail: Some(3), ..Default::default() });
        let mut ids = std::collections::BTreeSet::new();
        let mut now = 0;
        while now <= 80 * TICK_HZ {
            let tracks = if now < 2 * TICK_HZ { vec![track(1, 0.0), track(2, 300.0)] } else { vec![track(2, 300.0)] };
            for b in edge.tick(&tracks, &EgoInput::default(), now) {
                for r in Frame::decode(&b).unwrap().records { if let Record::Contact(c) = r { ids.insert(c.id); } }
            }
            now += 12;
        }
        assert_eq!(ids.len(), 1, "only the car that stayed is ever on the wire: {ids:?}");
        assert!(edge.cm.contacts.len() == 1, "the ghost's contact is gone from the edge too");
        assert_eq!(edge.last_ego.unwrap().group_m, m_to_m8(60.0));
    }

    #[test]
    fn paced_by_the_radio_the_edge_follows_the_link_not_the_budget() {
        // Told 9600 bit/s, the radio drains 600: frames shrink to the drained rate, and the crowd
        // coarsens just as it does when the budget is right.
        let mut edge = Edge::new(EdgeConfig { budget_bps: 9600, paced: true, ..Default::default() });
        assert_eq!(edge.cm.level, 1);
        let mut rx = crate::receiver::Receiver::new(9600);
        let (mut now, mut bytes) = (0, 0usize);
        while now <= 120 * TICK_HZ {
            edge.link_credit(75 * 12 / TICK_HZ, now);
            for b in edge.tick(&crowd(now), &EgoInput::default(), now) { bytes += b.len() + edge.cfg.carrier_overhead; rx.on_frame(&b).unwrap(); }
            if now % (5 * TICK_HZ) == 0 { digest(&mut edge, &mut rx, now); }
            now += 12;
        }
        assert!(bytes as f32 / 120.0 <= 80.0, "never more than the radio took: {} B/s", bytes as f32 / 120.0);
        assert!(edge.cm.level >= 3, "coarsened on the measured load: level {}", edge.cm.level);
    }

    #[test]
    fn focused_records_take_at_most_half_the_link() {
        let mut edge = Edge::new(EdgeConfig { budget_bps: 600, ..Default::default() });
        let tracks: Vec<Track> = (1..=4).map(|i| Track { class: 0, ..track(i, i as f32 * 3.0) }).collect();
        let mut now = 0;
        while now <= 10 * TICK_HZ { edge.tick(&tracks, &EgoInput::default(), now); now += 12; }
        let base = edge.timing.focus;
        assert_eq!(edge.focus_period(now), base, "nothing focused");
        let gid = edge.cm.tracks[&1].contact.unwrap();
        edge.cm.set_focus(gid, true, false);
        assert_eq!(edge.focus_period(now), base, "one focused contact keeps T_focus at 600 bit/s");
        edge.cm.set_focus(gid, true, true);
        edge.tick(&tracks, &EgoInput::default(), now);
        let p = edge.focus_period(now) as f32 / TICK_HZ as f32;
        assert!(p > 3.0 && p < 5.0, "four children at 600 bit/s share half the link: {p} s");
    }
}
