//! Timing from the budget (PROTOCOL.md §6): regime, the ladder, the floor, and the per-record
//! schedule entry. Pure functions so the receiver can derive the same expectations.

use crate::TICK_HZ;
use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum Regime { Video, Wide, Thin, Floor }

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct Timing {
    pub budget_bps: u32,
    pub f: f32,
    pub ladder: [u32; 5],
    pub floor: u32,
    pub focus: u32,
    pub ego: u32,
    pub session: u32,
    /// Ticks between `Pose` records; None outside the video and wide regimes.
    pub pose: Option<u32>,
    pub regime: Regime,
}

pub const LADDER_S: [f32; 5] = [0.0, 2.0, 6.0, 14.0, 30.0];
pub const LADDER_LEN: u8 = 5;

fn ticks(s: f32) -> u32 { (s * TICK_HZ as f32 + 0.5) as u32 }

pub fn timing(budget_bps: u32) -> Timing {
    let f = if budget_bps == 0 { 0.125 } else { (800.0 / budget_bps as f32).clamp(0.125, 8.0) };
    let regime = if budget_bps == 0 || budget_bps >= 64_000 { Regime::Video } else if budget_bps >= 8_000 { Regime::Wide } else if budget_bps >= 400 { Regime::Thin } else { Regime::Floor };
    let mut ladder = [0u32; 5];
    for i in 0..5 { ladder[i] = ticks(LADDER_S[i] * f); }
    Timing {
        budget_bps, f, ladder,
        floor: ticks((60.0 * f).max(10.0)),
        focus: ticks((1.0 * f).max(1.0)),
        ego: ticks((5.0 * f).max(1.0)),
        session: ticks((30.0 * f).max(5.0)),
        pose: match regime { Regime::Video => Some(TICK_HZ / 10), Regime::Wide => Some(TICK_HZ), _ => None },
        regime,
    }
}

/// Target frame size: about one second of link time, within [24, max_frame]; unlimited budgets
/// use the max.
pub fn target_frame_bytes(budget_bps: u32, max_frame: usize) -> usize {
    if budget_bps == 0 { return max_frame; }
    ((budget_bps / 8) as usize).clamp(24, max_frame)
}

/// Per-record schedule: where on the ladder it is and when it is due.
#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct Entry {
    pub step: u8,
    pub due: u32,
    pub last_sent: Option<u32>,
    pub sends: u32,
}
impl Entry {
    pub fn changed(&mut self, now: u32) { self.step = 0; self.due = now; }
    pub fn sent(&mut self, now: u32, t: &Timing, focused: bool) {
        self.last_sent = Some(now); self.sends += 1;
        if focused { self.due = now + t.focus; return; }
        self.step = (self.step + 1).min(LADDER_LEN);
        self.due = now + if (self.step as usize) < t.ladder.len() { t.ladder[self.step as usize] - t.ladder[self.step as usize - 1] } else { t.floor };
    }
    /// The receiver confirmed this revision: nothing but the floor repeat is needed.
    pub fn acked(&mut self, t: &Timing) {
        self.step = LADDER_LEN;
        self.due = self.last_sent.unwrap_or(self.due) + t.floor;
    }
    pub fn overdue(&self, now: u32) -> i64 { now as i64 - self.due as i64 }
}

/// Receiver side: the gap the edge is expected to leave before the next copy of a record whose
/// revision was first heard `since_rev` ticks ago (ladder gaps, then the floor).
pub fn expected_gap(since_rev: u32, t: &Timing) -> u32 {
    for i in 1..t.ladder.len() { if since_rev < t.ladder[i] { return t.ladder[i] - t.ladder[i - 1]; } }
    t.floor
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timing_scales_with_the_budget() {
        let t = timing(800);
        assert_eq!(t.f, 1.0); assert_eq!(t.regime, Regime::Thin);
        assert_eq!(t.ladder, [0, 240, 720, 1680, 3600]);
        assert_eq!(t.floor, 60 * TICK_HZ); assert_eq!(t.ego, 5 * TICK_HZ); assert_eq!(t.session, 30 * TICK_HZ); assert_eq!(t.focus, TICK_HZ);
        assert_eq!(t.pose, None);
        let fat = timing(0);
        assert_eq!(fat.regime, Regime::Video); assert_eq!(fat.pose, Some(12)); assert_eq!(fat.floor, 10 * TICK_HZ); assert_eq!(fat.ego, TICK_HZ);
        assert_eq!(timing(100_000).regime, Regime::Video);
        assert_eq!(timing(9_600).regime, Regime::Wide); assert_eq!(timing(9_600).pose, Some(TICK_HZ));
        assert_eq!(timing(2_000).regime, Regime::Thin);
        let slow = timing(100);
        assert_eq!(slow.regime, Regime::Floor); assert_eq!(slow.f, 8.0); assert_eq!(slow.floor, 480 * TICK_HZ); assert_eq!(slow.ego, 40 * TICK_HZ);
        assert_eq!(target_frame_bytes(800, 1200), 100); assert_eq!(target_frame_bytes(100, 1200), 24); assert_eq!(target_frame_bytes(0, 1200), 1200);
        assert_eq!(target_frame_bytes(100_000, 128), 128);
    }

    #[test]
    fn ladder_then_floor_then_ack() {
        let t = timing(800);
        let mut e = Entry::default();
        e.changed(100);
        assert_eq!(e.due, 100);
        let mut now = 100; let mut dues = vec![];
        for _ in 0..6 { e.sent(now, &t, false); dues.push(e.due - now); now = e.due; }
        assert_eq!(dues, vec![240, 480, 960, 1920, 7200, 7200], "gaps 2, 4, 8, 16 s then the 60 s floor");
        e.acked(&t);
        assert_eq!(e.step, LADDER_LEN);
        let mut f = Entry::default(); f.changed(0); f.sent(0, &t, true); assert_eq!(f.due, TICK_HZ);
        assert_eq!(expected_gap(0, &t), 240); assert_eq!(expected_gap(1000, &t), 960); assert_eq!(expected_gap(5000, &t), t.floor);
    }
}
