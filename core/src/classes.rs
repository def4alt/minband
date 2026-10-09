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

pub fn prior(class: u8) -> ClassPrior {
    match class {
        PERSON => PERSON_PRIOR,
        BACKPACK | HANDBAG | BOTTLE | CUP | CELL_PHONE => CARRIED_PRIOR,
        CHAIR | LAPTOP | TV => STATIC_PRIOR,
        _ => CARRIED_PRIOR,
    }
}
