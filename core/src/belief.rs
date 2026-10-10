//! The receiver's belief about where a contact is now, as a radius that holds the edge's estimate
//! with probability `COVERAGE` (PROTOCOL.md 5.3). Three sources, nothing else:
//!
//! 1. **The edge's guarantee.** The edge revises a contact as soon as its estimate leaves the band
//!    `thr` around the receiver's prediction of the last record it sent (`SentPred`, the same
//!    function on both ends), or its state changes. A receiver holding the latest copy is off by at
//!    most `thr` (plus the position quantum).
//! 2. **The link's evidence.** The receiver knows which frames it missed (sequence gaps), the loss
//!    rate, and when the edge's queue was empty (`cycle_end`). Up to the last empty-queue frame with
//!    no gap after the held copy, nothing was missed. After that, a revision issued `d` seconds ago
//!    is still unreceived with probability `p_loss ^ k(d)`, `k` the copies the ladder would have
//!    delivered before the last frame the receiver heard (nothing sent after that has arrived); the
//!    first unreceived revision starts the drift.
//! 3. **How things drift.** Once the receiver's prediction is stale, the edge's estimate wanders
//!    from it as measured on real footage (`drift`): log-normal at each lag, fitted per motion state
//!    and coarse class (median and 95th percentile as power laws of the lag), and at most the class
//!    speed cap times the lag. A lost contact drifts from its last look: nobody is watching it.
//!
//! Inside the band the receiver's copy drifts like everything else since its observation, but by
//! no more than the band (`Drift::quantile_within`). The radius is the smallest one whose miss
//! probability is `1 - COVERAGE`. It grows only for time
//! nobody can vouch for, and as fast as such things were seen to move. A radius wider than the
//! camera footprint no longer locates the contact (`located` in the receiver).

use crate::classes::{max_speed, COARSE_DISMOUNT, COARSE_VEHICLE, COARSE_ARMOUR};
use crate::wire::{MOTION_MOVING, MOTION_STATIC, MOTION_STOPPED};

/// Probability that the shown circle holds the edge's estimate.
pub const COVERAGE: f32 = 0.95;
/// Seconds from a revision to its first copy arriving when the link is free (frame build + delay).
pub const FIRST_COPY_S: f32 = 0.5;

/// Drift of the edge's estimate away from a stale prediction, `lag` seconds after it went stale.
/// Fitted on the footage runs (tools/sidebyside/scripts/drift.mjs on the footage runs, docs/PROTOCOL_EVAL.md 10):
/// median = `med_a * lag^med_b`, 95th percentile = `p95_a * lag^p95_b`, metres.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Drift { pub med_a: f32, pub med_b: f32, pub p95_a: f32, pub p95_b: f32, pub cap: f32 }

pub fn drift(motion: u8, coarse: u8) -> Drift {
    let person = coarse == COARSE_DISMOUNT;
    let (med_a, med_b, p95_a, p95_b) = match (motion, person) {
        (MOTION_MOVING, true) => (0.75, 0.95, 5.0, 0.78),
        (MOTION_MOVING, false) => (1.5, 0.95, 10.8, 0.93),
        (MOTION_STATIC, true) => (0.55, 0.83, 5.8, 0.51),
        (MOTION_STATIC, false) => (0.39, 0.85, 4.7, 0.40),
        (MOTION_STOPPED, true) => (0.78, 0.92, 6.6, 0.58),
        (MOTION_STOPPED, false) => (0.44, 0.80, 5.9, 0.35),
        (_, true) => (0.60, 0.87, 3.7, 0.57),
        (_, false) => (1.7, 0.48, 9.3, 0.46),
    };
    Drift { med_a, med_b, p95_a, p95_b, cap: max_speed(if coarse == COARSE_ARMOUR { COARSE_ARMOUR } else if person { COARSE_DISMOUNT } else { COARSE_VEHICLE }) }
}

/// Revisions per second while in a state (position, state, mix, ce, course, speed together), as
/// measured on the same runs: the rate at which the receiver's prediction can go stale.
pub fn revision_rate(motion: u8, coarse: u8) -> f32 {
    let person = coarse == COARSE_DISMOUNT;
    match (motion, person) {
        (MOTION_MOVING, _) => 0.85,
        (MOTION_STATIC, true) => 0.30,
        (MOTION_STATIC, false) => 0.25,
        (MOTION_STOPPED, true) => 0.35,
        (MOTION_STOPPED, false) => 0.20,
        (_, true) => 0.40,
        (_, false) => 0.55,
    }
}

impl Drift {
    fn median(&self, lag: f32) -> f32 { (self.med_a * lag.powf(self.med_b)).max(1e-3) }
    fn p95(&self, lag: f32) -> f32 { (self.p95_a * lag.powf(self.p95_b)).max(self.median(lag) * 1.01) }
    fn sigma(&self, lag: f32) -> f32 { ((self.p95(lag) / self.median(lag)).ln() / 1.644_854).max(0.05) }
    /// P(drift after `lag` s > r).
    pub fn exceeds(&self, lag: f32, r: f32) -> f32 {
        if lag <= 0.0 { return 0.0; }
        if r >= self.cap * lag + 1.0 { return 0.0; }
        if r <= 0.0 { return 1.0; }
        1.0 - normal_cdf((r.ln() - self.median(lag).ln()) / self.sigma(lag))
    }
    /// The radius drift stays inside with probability `p` after `lag` s, given that it stayed
    /// inside `limit` (the edge revises past its band, so a copy nobody revised is inside it).
    pub fn quantile_within(&self, lag: f32, p: f32, limit: f32) -> f32 {
        if lag <= 0.0 || limit <= 0.0 { return 0.0; }
        let z = (limit.ln() - self.median(lag).ln()) / self.sigma(lag);
        let f = normal_cdf(z) * p;
        if f <= 1e-6 { return 0.0; }
        (self.median(lag) * (self.sigma(lag) * normal_quantile(f)).exp()).min(limit)
    }
    /// The radius drift stays inside with probability `p` after `lag` s.
    pub fn quantile(&self, lag: f32, p: f32) -> f32 {
        if lag <= 0.0 { return 0.0; }
        (self.median(lag) * (self.sigma(lag) * normal_quantile(p)).exp()).min(self.cap * lag + 1.0)
    }
}

/// What the receiver knows about the link for one held copy.
#[derive(Clone, Copy, Debug)]
pub struct LinkEvidence {
    /// Seconds since the receiver can vouch for this copy (last empty-queue frame with no gap after
    /// the copy, else the copy's own frame). 0 = vouched for up to now.
    pub unassured_s: f32,
    /// Measured frame loss rate.
    pub p_loss: f32,
    /// Seconds since the last frame of any kind arrived: no copy sent after that has arrived.
    pub silent_s: f32,
    /// Seconds fresh news waits in the edge's queue (Ego.backlog): copies go out that much later.
    pub backlog_s: f32,
    /// Seconds after a revision at which the ladder sends copies: 0, then the ladder and floor.
    pub copy_offsets: [f32; 8],
}

/// Extra radius beyond the guarantee band for a tracked contact, and the probability that the
/// receiver missed a revision at all.
pub fn tracked_extra(d: &Drift, rate: f32, link: &LinkEvidence) -> (f32, f32) {
    let w = link.unassured_s;
    if w <= 0.0 { return (0.0, 0.0); }
    // First unreceived revision at lag `x` before now: density rate * pu(x) * survival of earlier ones.
    let pu = |x: f32| -> f32 {
        // Copies that would have arrived before the last frame we heard; each was lost with p_loss.
        let k = link.copy_offsets.iter().filter(|&&o| o + FIRST_COPY_S + link.backlog_s + link.silent_s <= x).count() as i32;
        link.p_loss.clamp(0.0, 1.0).powi(k)
    };
    const N: usize = 64;
    let dx = w / N as f32;
    let mut lags = [0f32; N]; let mut dens = [0f32; N];
    let mut h = 0.0f32;
    // Oldest first: the first unreceived revision is the earliest one.
    for i in 0..N {
        let x = w - (i as f32 + 0.5) * dx;
        let lam = rate * pu(x);
        dens[i] = lam * (-h).exp() * dx;
        lags[i] = x;
        h += lam * dx;
    }
    let p_miss = 1.0 - (-h).exp();
    let tail = 1.0 - COVERAGE;
    if p_miss <= tail { return (0.0, p_miss); }
    let p_over = |r: f32| -> f32 { (0..N).map(|i| dens[i] * d.exceeds(lags[i], r)).sum() };
    let (mut lo, mut hi) = (0.0f32, d.cap * w + 1.0);
    for _ in 0..30 { let mid = 0.5 * (lo + hi); if p_over(mid) > tail { lo = mid; } else { hi = mid; } }
    (hi, p_miss)
}

/// Radius for a contact the edge lost `since_look` s ago: nobody watches it, it drifts.
pub fn lost_radius(d: &Drift, since_look: f32) -> f32 { d.quantile(since_look, COVERAGE) }

/// Standard normal CDF (Abramowitz-Stegun 7.1.26 erf, |error| < 1.5e-7).
pub fn normal_cdf(x: f32) -> f32 {
    let z = x as f64 / std::f64::consts::SQRT_2;
    let t = 1.0 / (1.0 + 0.327_591_1 * z.abs());
    let y = 1.0 - (((((1.061_405_429 * t - 1.453_152_027) * t) + 1.421_413_741) * t - 0.284_496_736) * t + 0.254_829_592) * t * (-z * z).exp();
    (0.5 * (1.0 + if z >= 0.0 { y } else { -y })) as f32
}

/// Inverse standard normal CDF (Acklam's rational approximation, relative error < 1.2e-9).
pub fn normal_quantile(p: f32) -> f32 {
    let p = (p as f64).clamp(1e-12, 1.0 - 1e-12);
    const A: [f64; 6] = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.383577518672690e2, -3.066479806614716e1, 2.506628277459239];
    const B: [f64; 5] = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1];
    const C: [f64; 6] = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
    const D: [f64; 4] = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416];
    let pl = 0.02425;
    let x = if p < pl {
        let q = (-2.0 * p.ln()).sqrt();
        (((((C[0] * q + C[1]) * q + C[2]) * q + C[3]) * q + C[4]) * q + C[5]) / ((((D[0] * q + D[1]) * q + D[2]) * q + D[3]) * q + 1.0)
    } else if p <= 1.0 - pl {
        let q = p - 0.5; let r = q * q;
        (((((A[0] * r + A[1]) * r + A[2]) * r + A[3]) * r + A[4]) * r + A[5]) * q / (((((B[0] * r + B[1]) * r + B[2]) * r + B[3]) * r + B[4]) * r + 1.0)
    } else {
        let q = (-2.0 * (1.0 - p).ln()).sqrt();
        -(((((C[0] * q + C[1]) * q + C[2]) * q + C[3]) * q + C[4]) * q + C[5]) / ((((D[0] * q + D[1]) * q + D[2]) * q + D[3]) * q + 1.0)
    };
    x as f32
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wire::MOTION_UNKNOWN;

    #[test]
    fn normal_helpers_round_trip() {
        for p in [0.01f32, 0.25, 0.5, 0.75, 0.95, 0.99] { assert!((normal_cdf(normal_quantile(p)) - p).abs() < 1e-4, "{p}"); }
        assert!((normal_quantile(0.95) - 1.6449).abs() < 1e-3);
    }

    #[test]
    fn drift_quantiles_match_the_fit() {
        let d = drift(MOTION_MOVING, COARSE_VEHICLE);
        assert!((d.quantile(5.0, 0.95) - 10.8 * 5f32.powf(0.93)).abs() < 0.5);
        assert!((d.quantile(5.0, 0.5) - 1.5 * 5f32.powf(0.95)).abs() < 0.2);
        assert!((d.exceeds(5.0, d.quantile(5.0, 0.95)) - 0.05).abs() < 1e-3);
        assert!(d.quantile(1000.0, 0.95) <= d.cap * 1000.0 + 1.0);
        let _ = drift(MOTION_UNKNOWN, COARSE_DISMOUNT);
    }

    fn link(unassured_s: f32, p_loss: f32) -> LinkEvidence { LinkEvidence { unassured_s, p_loss, silent_s: 0.0, backlog_s: 0.0, copy_offsets: [0.0, 0.8, 2.4, 5.6, 12.0, 36.0, 60.0, 84.0] } }

    #[test]
    fn vouched_for_means_no_extra_and_loss_grows_it() {
        let d = drift(MOTION_STATIC, COARSE_VEHICLE);
        let r = revision_rate(MOTION_STATIC, COARSE_VEHICLE);
        assert_eq!(tracked_extra(&d, r, &link(0.0, 0.1)).0, 0.0);
        // A quiet second on a clean link: a revision would have arrived; nothing to add.
        let (a, pa) = tracked_extra(&d, r, &link(1.0, 0.0));
        assert!(pa < 0.15, "{pa}");
        // Thirty seconds with frames lost at 50 %: the ladder's copies get most news through, but
        // not all: about one chance in two that a revision is still missing, and the radius grows.
        let (b, pb) = tracked_extra(&d, r, &link(30.0, 0.5));
        assert!(pb > 0.3 && b > a && b > 3.0, "{a} {b} {pb}");
        // With every frame lost the same thirty seconds are a near-certain miss.
        let (_, pc) = tracked_extra(&d, r, &link(30.0, 1.0));
        assert!(pc > 0.99, "{pc}");
        // Moving vehicles drift much faster than parked ones over the same unknown time.
        let m = drift(MOTION_MOVING, COARSE_VEHICLE);
        let (c, _) = tracked_extra(&m, revision_rate(MOTION_MOVING, COARSE_VEHICLE), &link(30.0, 0.5));
        assert!(c > 3.0 * b, "{b} {c}");
    }

    #[test]
    fn lost_contacts_drift_from_the_last_look() {
        let parked = drift(MOTION_STATIC, COARSE_VEHICLE);
        let moving = drift(MOTION_MOVING, COARSE_VEHICLE);
        assert!(lost_radius(&parked, 30.0) < 20.0, "{}", lost_radius(&parked, 30.0));
        assert!(lost_radius(&moving, 30.0) > 150.0, "{}", lost_radius(&moving, 30.0));
        assert!(lost_radius(&parked, 10.0) < lost_radius(&parked, 60.0));
    }
}
