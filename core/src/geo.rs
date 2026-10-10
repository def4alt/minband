//! ENU geometry on the edge: observation rays, the error radius, and a few deterministic helpers
//! the receiver's dead reckoning needs (sin/cos from basic f32 ops only, so both ends agree bit
//! for bit).

/// Bearing (degrees, true, 0..360) and depression (degrees below the horizon) of the ray from the
/// camera at (ce, cn, cu) to the ground point (pe, pn, pu).
pub fn ray_az_el(ce: f32, cn: f32, cu: f32, pe: f32, pn: f32, pu: f32) -> (f32, f32) {
    let (de, dn, du) = (pe - ce, pn - cn, pu - cu);
    let horiz = (de * de + dn * dn).sqrt();
    let mut az = de.atan2(dn).to_degrees();
    if az < 0.0 { az += 360.0; }
    let el = (-du).atan2(horiz).to_degrees();
    (az, el)
}

/// Horizontal error radius (1 sigma, metres) of a ground point seen at ground range `range` and
/// depression `el_deg`: own position, attitude at range, height through the depression angle, and
/// the pixel error of the box foot at range (DESIGN.md §3.1).
pub fn ce_m(range: f32, el_deg: f32, sigma_own: f32, sigma_att_deg: f32, sigma_h: f32, sigma_px: f32, f_px: f32) -> f32 {
    let el = el_deg.clamp(2.0, 90.0).to_radians();
    let att = range * sigma_att_deg.to_radians();
    let h = sigma_h / el.tan();
    let px = if f_px > 0.0 { sigma_px * range / f_px } else { 0.0 };
    (sigma_own * sigma_own + att * att + h * h + px * px).sqrt()
}

/// Deterministic sin and cos of an angle in degrees (f32 + - * only, range-reduced Taylor to the
/// 11th power: error < 1e-6 over the reduced range).
pub fn sincos_deg(deg: f32) -> (f32, f32) {
    let mut x = deg * (core::f32::consts::PI / 180.0);
    // Reduce to [-pi, pi].
    let two_pi = 2.0 * core::f32::consts::PI;
    while x > core::f32::consts::PI { x -= two_pi; }
    while x < -core::f32::consts::PI { x += two_pi; }
    // Fold to [-pi/2, pi/2] with sign bookkeeping.
    let mut sign_cos = 1.0f32;
    if x > core::f32::consts::FRAC_PI_2 { x = core::f32::consts::PI - x; sign_cos = -1.0; }
    else if x < -core::f32::consts::FRAC_PI_2 { x = -core::f32::consts::PI - x; sign_cos = -1.0; }
    let x2 = x * x;
    let s = x * (1.0 - x2 / 6.0 * (1.0 - x2 / 20.0 * (1.0 - x2 / 42.0 * (1.0 - x2 / 72.0 * (1.0 - x2 / 110.0)))));
    let c = 1.0 - x2 / 2.0 * (1.0 - x2 / 12.0 * (1.0 - x2 / 30.0 * (1.0 - x2 / 56.0 * (1.0 - x2 / 90.0))));
    (s, sign_cos * c)
}

/// Course over ground in degrees (0 = north, 90 = east) and speed of an ENU velocity.
pub fn course_speed(ve: f32, vn: f32) -> (f32, f32) {
    let speed = (ve * ve + vn * vn).sqrt();
    let mut crs = ve.atan2(vn).to_degrees();
    if crs < 0.0 { crs += 360.0; }
    (crs, speed)
}

/// Equirectangular ENU -> WGS84 degrees around an origin (degrees). Good to ~1 m within a few km,
/// enough for display; the server has the exact conversion.
pub fn enu_to_latlon(origin_lat: f64, origin_lon: f64, e: f64, n: f64) -> (f64, f64) {
    const R: f64 = 6371008.8;
    let lat = origin_lat + (n / R).to_degrees();
    let lon = origin_lon + (e / (R * origin_lat.to_radians().cos())).to_degrees();
    (lat, lon)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rays_point_the_right_way() {
        let (az, el) = ray_az_el(0.0, 0.0, 80.0, 0.0, 80.0, 0.0);
        assert!((az - 0.0).abs() < 1e-3 && (el - 45.0).abs() < 1e-3, "{az} {el}");
        let (az, _) = ray_az_el(0.0, 0.0, 80.0, 50.0, 0.0, 0.0);
        assert!((az - 90.0).abs() < 1e-3);
        let (az, el) = ray_az_el(10.0, 10.0, 80.0, 10.0, 10.0, 0.0);
        assert!((el - 90.0).abs() < 1e-3, "{az} {el}");
    }

    #[test]
    fn ce_grows_with_range_and_shallow_angles() {
        let near = ce_m(50.0, 60.0, 3.0, 1.0, 2.0, 2.0, 2000.0);
        let far = ce_m(300.0, 15.0, 3.0, 1.0, 2.0, 2.0, 2000.0);
        assert!(near > 3.0 && near < 6.0, "{near}");
        assert!(far > 8.0 && far > near * 2.0, "{far}");
        // Consumer parts at 100 m AGL, 45 degrees: 5-8 m (DESIGN.md).
        let typical = ce_m(100.0, 45.0, 3.0, 1.5, 2.0, 2.0, 2000.0);
        assert!(typical > 4.0 && typical < 8.0, "{typical}");
    }

    #[test]
    fn sincos_matches_std() {
        for d in (-720..=720).step_by(7) {
            let (s, c) = sincos_deg(d as f32);
            let (s0, c0) = (d as f32).to_radians().sin_cos();
            assert!((s - s0).abs() < 2e-6 && (c - c0).abs() < 2e-6, "{d}: {s} {c} vs {s0} {c0}");
        }
        let (crs, spd) = course_speed(3.0, 0.0);
        assert!((crs - 90.0).abs() < 1e-4 && (spd - 3.0).abs() < 1e-6);
        let (crs, _) = course_speed(0.0, -1.0);
        assert!((crs - 180.0).abs() < 1e-4);
    }

    #[test]
    fn enu_to_latlon_scale() {
        let (lat, lon) = enu_to_latlon(39.35, -85.70, 1000.0, 1000.0);
        assert!((lat - 39.35 - 0.008993).abs() < 1e-4, "{lat}");
        assert!((lon + 85.70 - 0.011633).abs() < 2e-4, "{lon}");
    }
}
