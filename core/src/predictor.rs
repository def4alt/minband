//! Per-class kinematic extrapolation. Must be bit-identical on every platform, so only
//! +, -, *, /, sqrt and comparisons on f32 are used (all correctly rounded under IEEE-754).

use crate::classes::prior;
use crate::wire::EntityState;
use crate::TICK_HZ;

pub struct Predictor;

impl Predictor {
    /// Extrapolate `s` from `s.tick` to `to_tick`. Ticks before `s.tick` return `s` unchanged.
    pub fn step(s: &EntityState, to_tick: u32) -> EntityState {
        if to_tick <= s.tick {
            return *s;
        }
        let p = prior(s.class);
        let dt = (to_tick - s.tick) as f32 / TICK_HZ as f32;

        // Linearised damping: v(t) = v0 * max(0, 1 - (1 - d) * dt). Avoids powf.
        let k = 1.0 - (1.0 - p.damping_per_s) * dt;
        let k = if k < 0.0 { 0.0 } else { k };
        let mut vel = [s.vel[0] * k, s.vel[1] * k, s.vel[2] * k];

        let speed2 = vel[0] * vel[0] + vel[1] * vel[1] + vel[2] * vel[2];
        let max2 = p.max_speed * p.max_speed;
        if speed2 > max2 {
            let scale = p.max_speed / speed2.sqrt();
            vel = [vel[0] * scale, vel[1] * scale, vel[2] * scale];
        }

        // Trapezoidal integration between old and damped velocity.
        let mut pos = [
            s.pos[0] + (s.vel[0] + vel[0]) * 0.5 * dt,
            s.pos[1] + (s.vel[1] + vel[1]) * 0.5 * dt,
            s.pos[2] + (s.vel[2] + vel[2]) * 0.5 * dt,
        ];
        if let Some(g) = p.ground_y {
            if pos[1] < g {
                pos[1] = g;
                if vel[1] < 0.0 {
                    vel[1] = 0.0;
                }
            }
        }
        EntityState { pos, vel, tick: to_tick, ..*s }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::classes::PERSON;

    fn person(pos: [f32; 3], vel: [f32; 3]) -> EntityState {
        EntityState { id: 1, class: PERSON, pos, vel, conf: 255, tick: 0 }
    }

    #[test]
    fn moves_forward_with_damping() {
        let s = person([0.0, 0.0, 0.0], [1.0, 0.0, 0.0]);
        let out = Predictor::step(&s, TICK_HZ); // one second
        assert!(out.pos[0] > 0.9 && out.pos[0] < 1.0);
        assert!(out.vel[0] < 1.0 && out.vel[0] > 0.8);
        assert_eq!(out.tick, TICK_HZ);
    }

    #[test]
    fn clamps_to_ground_and_speed() {
        let s = person([0.0, 0.5, 0.0], [10.0, -5.0, 0.0]);
        let out = Predictor::step(&s, TICK_HZ);
        assert_eq!(out.pos[1], 0.0);
        let speed = (out.vel[0] * out.vel[0] + out.vel[1] * out.vel[1]).sqrt();
        assert!(speed <= 3.0 + 1e-5);
    }

    #[test]
    fn past_tick_is_identity() {
        let s = person([1.0, 2.0, 3.0], [1.0, 0.0, 0.0]);
        let mut s2 = s;
        s2.tick = 10;
        assert_eq!(Predictor::step(&s2, 5), s2);
    }
}
