//! Class ids on the wire are COCO indices plus MinBand's own from 100 up. The protocol only carries
//! the coarse class mix (dismount / vehicle / armour / other); the fine class drives the motion
//! thresholds and the dead-reckoning speed cap.

pub const PERSON: u8 = 0;
pub const BICYCLE: u8 = 1;
pub const CAR: u8 = 2;
pub const MOTORCYCLE: u8 = 3;
pub const BUS: u8 = 5;
pub const TRUCK: u8 = 7;
/// Unclassified ground mover (motion detection, tools/footage/mti.py).
pub const MOVER: u8 = 100;
/// Armoured vehicle, only when an appearance model actually provides it.
pub const ARMOURED: u8 = 101;

pub const COARSE_DISMOUNT: u8 = 0;
pub const COARSE_VEHICLE: u8 = 1;
pub const COARSE_ARMOUR: u8 = 2;
pub const COARSE_OTHER: u8 = 3;

pub fn coarse(class: u8) -> u8 {
    match class {
        PERSON => COARSE_DISMOUNT,
        BICYCLE | CAR | MOTORCYCLE | BUS | TRUCK => COARSE_VEHICLE,
        ARMOURED => COARSE_ARMOUR,
        _ => COARSE_OTHER,
    }
}

pub fn coarse_name(c: u8) -> &'static str {
    match c { COARSE_DISMOUNT => "dismount", COARSE_VEHICLE => "vehicle", COARSE_ARMOUR => "armour", _ => "other" }
}

/// Dead-reckoning speed cap per coarse class (m/s): a dismount cannot be extrapolated at 20 m/s.
pub fn max_speed(coarse: u8) -> f32 {
    match coarse { COARSE_DISMOUNT => 3.0, COARSE_VEHICLE | COARSE_ARMOUR => 30.0, _ => 25.0 }
}

/// Motion thresholds (m/s): above `hi` for 2 s is moving, below `lo` for 5 s is stopped/static.
pub fn motion_thresholds(coarse: u8) -> (f32, f32) {
    match coarse { COARSE_DISMOUNT => (0.5, 0.2), _ => (0.7, 0.3) }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn coarse_classes() {
        assert_eq!(coarse(PERSON), COARSE_DISMOUNT);
        for c in [BICYCLE, CAR, MOTORCYCLE, BUS, TRUCK] { assert_eq!(coarse(c), COARSE_VEHICLE); }
        assert_eq!(coarse(ARMOURED), COARSE_ARMOUR);
        assert_eq!(coarse(MOVER), COARSE_OTHER);
        assert_eq!(coarse(56), COARSE_OTHER);
        assert!(max_speed(COARSE_DISMOUNT) < max_speed(COARSE_OTHER));
    }
}
