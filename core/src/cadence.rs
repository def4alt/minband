//! Heartbeat cadence derived from the byte budget (S19). The keyframe is the trust mechanism: under
//! a prediction-error trigger, silence means "within threshold" only while the heartbeat keeps
//! arriving, so its period is a link-class parameter (DIS 5 s, Iridium SBD one message per 10-15 s).
//! Both ends derive it from the same budget: the edge from the budget it was given (`Ack` or
//! `set_budget`), the receiver from the budget it last advertised in `make_ack`. Integer math only.
//!
//! | budget bit/s | keyframe | hello | pose | coast | stale | drop |
//! |---|---|---|---|---|---|---|
//! | 0 (unlimited), >= 16000 | 2 s | 5 s | 0.5 s | 2.5 s | 6 s | 10 s |
//! | 8000 | 2 s | 5 s | 1 s | 2.5 s | 6 s | 10 s |
//! | 4000 | 3 s | 6 s | 2 s | 3.75 s | 9 s | 15 s |
//! | 1500 | 6.33 s | 12.7 s | 10 s | 7.9 s | 19 s | 31.7 s |
//! | 600 | 14.3 s | 28.7 s | 10 s | 17.9 s | 43 s | 71.7 s |
//! | <= 571 | 15 s | 30 s | 10 s | 18.75 s | 45 s | 75 s |

use crate::TICK_HZ;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cadence {
    /// Edge: periodic keyframe (also sent when nothing is tracked, as the heartbeat).
    pub keyframe_ticks: u32,
    /// Edge: Hello re-sent after being acked, so a restarted server re-identifies the device.
    pub hello_refresh_ticks: u32,
    /// Edge: minimum interval between `Pose` datagrams (cosmetic: the viewer's frustum).
    pub pose_ticks: u32,
    /// Receiver: device silent this long -> its entities are coasting (one missed keyframe).
    pub coast_ticks: u32,
    /// Receiver: entity not refreshed this long is stale; device silent this long may be gc'd.
    pub stale_ticks: u32,
    /// Receiver: entity not refreshed this long is dropped once its device is silent.
    pub drop_ticks: u32,
}

/// Keyframe period: 1 s plus the time the budget needs for 1 kB, i.e. a 3-entity keyframe
/// (~130 B on the wire) costs at most ~12 % of any budget. 2 s at >= 8000 bit/s, ~14 s at 600.
const KF_REF_BITS: u64 = 8000;
const KF_MIN: u32 = 2 * TICK_HZ;
const KF_MAX: u32 = 15 * TICK_HZ;
const HELLO_MIN: u32 = 5 * TICK_HZ;
const HELLO_MAX: u32 = 30 * TICK_HZ;
/// Pose (~64 B on the wire) every `8000 / budget` s is ~6 % of the budget; 0.5 s at >= 16 kbit/s.
const POSE_MIN: u32 = TICK_HZ / 2;
const POSE_MAX_FAST: u32 = 2 * TICK_HZ;
/// Below this budget the frustum is refreshed every 10 s: the bytes belong to the entities.
const POSE_SLOW_BELOW_BPS: u32 = 4000;
const POSE_SLOW: u32 = 10 * TICK_HZ;

impl Cadence {
    /// `{"keyframeTicks":..,"helloRefreshTicks":..,"poseTicks":..,"coastTicks":..,"staleTicks":..,"dropTicks":..}`
    pub fn json(&self) -> String {
        format!(
            "{{\"keyframeTicks\":{},\"helloRefreshTicks\":{},\"poseTicks\":{},\"coastTicks\":{},\"staleTicks\":{},\"dropTicks\":{}}}",
            self.keyframe_ticks, self.hello_refresh_ticks, self.pose_ticks, self.coast_ticks, self.stale_ticks, self.drop_ticks
        )
    }
}

/// The cadence for a budget in bit/s (0 = unlimited, which equals an infinite budget and the
/// `EdgeConfig`/`ReceiverConfig` defaults). Every field is non-increasing in the budget.
pub fn cadence(budget_bps: u32) -> Cadence {
    let (keyframe, pose) = if budget_bps == 0 {
        (KF_MIN, POSE_MIN)
    } else {
        let b = budget_bps as u64;
        let kf = (TICK_HZ as u64 + KF_REF_BITS * TICK_HZ as u64 / b).clamp(KF_MIN as u64, KF_MAX as u64) as u32;
        let pose = if budget_bps < POSE_SLOW_BELOW_BPS {
            POSE_SLOW
        } else {
            (8000 * TICK_HZ as u64 / b).clamp(POSE_MIN as u64, POSE_MAX_FAST as u64) as u32
        };
        (kf, pose)
    };
    Cadence {
        keyframe_ticks: keyframe,
        hello_refresh_ticks: (2 * keyframe).clamp(HELLO_MIN, HELLO_MAX),
        pose_ticks: pose,
        // One keyframe period plus a margin for delivery jitter and one ack round trip.
        coast_ticks: keyframe + (keyframe / 4).max(TICK_HZ / 2),
        stale_ticks: (3 * keyframe).max(6 * TICK_HZ),
        drop_ticks: (5 * keyframe).max(10 * TICK_HZ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::edge::EdgeConfig;
    use crate::receiver::ReceiverConfig;

    #[test]
    fn unlimited_is_todays_defaults_and_an_infinite_budget() {
        let c = cadence(0);
        let (e, r) = (EdgeConfig::default(), ReceiverConfig::default());
        assert_eq!((c.keyframe_ticks, c.hello_refresh_ticks, c.pose_ticks), (e.keyframe_ticks, e.hello_refresh_ticks, e.pose_ticks));
        assert_eq!((c.coast_ticks, c.stale_ticks, c.drop_ticks), (r.coast_ticks, r.stale_ticks, r.drop_ticks));
        assert_eq!(c, cadence(u32::MAX));
        assert_eq!((c.keyframe_ticks, c.hello_refresh_ticks, c.pose_ticks, c.coast_ticks), (240, 600, 60, 300));
        assert_eq!(c.json(), r#"{"keyframeTicks":240,"helloRefreshTicks":600,"poseTicks":60,"coastTicks":300,"staleTicks":720,"dropTicks":1200}"#);
    }

    #[test]
    fn endpoints() {
        let hf = cadence(8000);
        assert_eq!((hf.keyframe_ticks, hf.hello_refresh_ticks, hf.pose_ticks), (2 * TICK_HZ, 5 * TICK_HZ, TICK_HZ));
        assert_eq!((hf.stale_ticks, hf.drop_ticks), (6 * TICK_HZ, 10 * TICK_HZ));
        let t = cadence(600);
        assert_eq!(t.keyframe_ticks, 1720, "~14.3 s at 600 bit/s");
        assert_eq!(t.pose_ticks, 10 * TICK_HZ);
        assert!(t.hello_refresh_ticks >= t.keyframe_ticks && t.hello_refresh_ticks <= 30 * TICK_HZ);
        assert_eq!(t.coast_ticks, 1720 + 430);
        assert_eq!((t.stale_ticks, t.drop_ticks), (3 * 1720, 5 * 1720));
        let floor = cadence(1);
        assert_eq!((floor.keyframe_ticks, floor.hello_refresh_ticks), (15 * TICK_HZ, 30 * TICK_HZ));
        assert_eq!(cadence(4000).pose_ticks, 2 * TICK_HZ);
        assert_eq!(cadence(16000).pose_ticks, TICK_HZ / 2);
    }

    #[test]
    fn monotone_in_budget_and_coherent() {
        let mut prev = cadence(1);
        for b in (2..20_000).chain([50_000, 1_000_000, u32::MAX]) {
            let c = cadence(b);
            assert!(c.keyframe_ticks <= prev.keyframe_ticks && c.hello_refresh_ticks <= prev.hello_refresh_ticks, "{b}");
            assert!(c.pose_ticks <= prev.pose_ticks && c.coast_ticks <= prev.coast_ticks, "{b}");
            assert!(c.stale_ticks <= prev.stale_ticks && c.drop_ticks <= prev.drop_ticks, "{b}");
            assert!(c.hello_refresh_ticks >= c.keyframe_ticks && c.hello_refresh_ticks >= 5 * TICK_HZ);
            assert!(c.coast_ticks >= c.keyframe_ticks + TICK_HZ / 2 && c.coast_ticks < c.stale_ticks && c.stale_ticks < c.drop_ticks);
            // Smooth: one bit/s never moves the keyframe period by more than a few ticks.
            assert!(prev.keyframe_ticks - c.keyframe_ticks <= 4, "{b}");
            prev = c;
        }
    }
}
