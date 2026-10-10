//! Class ids on the wire are COCO indices; this is the subset the demo tracks plus the per-class
//! motion priors the predictor needs.

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ClassPrior {
    /// Damping applied to velocity per second, as a multiplier (1.0 = none). Intent-driven
    /// movers (people) keep speed; placed objects (chairs, cups) decay to rest quickly.
    pub damping_per_s: f32,
    /// Hard speed cap in m/s.
    pub max_speed: f32,
    /// Clamp y to >= ground_y (metres above marker plane). None = free (e.g. drones).
    pub ground_y: Option<f32>,
}

pub const PERSON: u8 = 0;
pub const BICYCLE: u8 = 1;
pub const CAR: u8 = 2;
pub const MOTORCYCLE: u8 = 3;
pub const BUS: u8 = 5;
pub const TRUCK: u8 = 7;
pub const BACKPACK: u8 = 24;
pub const HANDBAG: u8 = 26;
pub const BOTTLE: u8 = 39;
pub const CUP: u8 = 41;
pub const CHAIR: u8 = 56;
pub const LAPTOP: u8 = 63;
pub const CELL_PHONE: u8 = 67;
pub const TV: u8 = 62;

const PERSON_PRIOR: ClassPrior = ClassPrior { damping_per_s: 0.85, max_speed: 3.0, ground_y: Some(0.0) };
const CARRIED_PRIOR: ClassPrior = ClassPrior { damping_per_s: 0.6, max_speed: 3.0, ground_y: None };
const STATIC_PRIOR: ClassPrior = ClassPrior { damping_per_s: 0.1, max_speed: 1.0, ground_y: None };
/// Seen from the air (real drone footage, tools/footage): vehicles keep their speed and stay on the
/// ground; 30 m/s covers urban and rural roads. Without this they fell through to the carried-object
/// prior and were capped at 3 m/s, so every moving car cost a delta per frame.
const VEHICLE_PRIOR: ClassPrior = ClassPrior { damping_per_s: 0.95, max_speed: 30.0, ground_y: Some(0.0) };
const CYCLE_PRIOR: ClassPrior = ClassPrior { damping_per_s: 0.9, max_speed: 12.0, ground_y: Some(0.0) };

pub fn prior(class: u8) -> ClassPrior {
    match class {
        PERSON => PERSON_PRIOR,
        BACKPACK | HANDBAG | BOTTLE | CUP | CELL_PHONE => CARRIED_PRIOR,
        CHAIR | LAPTOP | TV => STATIC_PRIOR,
        CAR | MOTORCYCLE | BUS | TRUCK => VEHICLE_PRIOR,
        BICYCLE => CYCLE_PRIOR,
        _ => CARRIED_PRIOR,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::predictor::Predictor;
    use crate::wire::EntityState;
    use crate::TICK_HZ;

    #[test]
    fn vehicles_keep_road_speed() {
        let car = EntityState { id: 1, class: CAR, pos: [0.0; 3], vel: [12.0, 0.0, 0.0], conf: 200, tick: 0 };
        let s = Predictor::step(&car, TICK_HZ);
        assert!(s.vel[0] > 11.0 && s.pos[0] > 11.0, "a car at 12 m/s is not capped to the 3 m/s carried prior: {:?}", s);
        assert_eq!(prior(TRUCK), prior(CAR));
        assert!(prior(BICYCLE).max_speed < prior(CAR).max_speed);
        assert_eq!(prior(PERSON).max_speed, 3.0, "dismounts unchanged");
    }
}
