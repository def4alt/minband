import SwiftUI

/// Standby backdrop: a wireframe contour mountain (the poster's terrain) in quiet hairlines on
/// `bg`. Rows of constant depth are drawn far to near, each one first filling the area below it
/// with `bg`, so nearer ridges hide the lines behind them (hidden-line removal without a depth
/// buffer). One layer, dim, and static: STYLE.md "Restraint" allows no idle animation and keeps
/// the terrain below the readouts in tone. Opaque: it also covers the AR view's frozen last
/// camera frame after STOP.
struct ContourField: View {
    static let rows = 40
    static let cols = 72

    var body: some View {
        Canvas { ctx, size in
            ctx.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Theme.bg))
            Self.draw(in: &ctx, size: size)
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    /// Terrain height (metres) at ground position (x right, z away from the viewer).
    static func height(_ x: Double, _ z: Double) -> Double {
        func bump(_ cx: Double, _ cz: Double, _ h: Double, _ sx: Double, _ sz: Double) -> Double {
            let dx = (x - cx) / sx, dz = (z - cz) / sz
            return h * exp(-(dx * dx + dz * dz))
        }
        // One main peak, a shoulder and two far ridges; the near field stays low and flat.
        var h = bump(0.6, 11.5, 3.1, 1.9, 2.4)
            + bump(-0.9, 13.5, 1.8, 2.2, 2.0)
            + bump(-5.0, 15.0, 2.0, 2.8, 3.0)
            + bump(5.4, 14.0, 1.6, 2.4, 2.6)
            + bump(2.8, 8.0, 0.55, 1.4, 1.2)
        // Ridged detail, scaled with the relief so the flats stay calm.
        let relief = 0.06 + 0.18 * min(1, h / 1.5)
        h += relief * sin(1.9 * x + 0.7 * z) * cos(1.3 * z - 0.6 * x)
        h += 0.04 * sin(3.7 * x - 2.3 * z)
        return h
    }

    static func draw(in ctx: inout GraphicsContext, size: CGSize) {
        let w = Double(size.width), hgt = Double(size.height)
        let horizon = hgt * 0.50          // eye-level line
        let f = w * 0.95                  // focal length in points
        let eye = 1.7                     // eye height above the base plane (m)
        let zNear = 3.0, zFar = 26.0
        for r in 0..<rows {
            // Far to near; uniform in 1/z so rows are evenly spaced on screen.
            let s = Double(r) / Double(rows - 1)
            let z = 1 / (1 / zFar + (1 / zNear - 1 / zFar) * s)
            let halfSpan = (w / 2 + 24) * z / f
            var line = Path()
            for c in 0...cols {
                let x = -halfSpan + 2 * halfSpan * Double(c) / Double(cols)
                let p = CGPoint(x: w / 2 + x * f / z, y: horizon + (eye - height(x, z)) * f / z)
                if c == 0 { line.move(to: p) } else { line.addLine(to: p) }
            }
            var fill = line
            fill.addLine(to: CGPoint(x: w + 30, y: hgt + 10))
            fill.addLine(to: CGPoint(x: -30, y: hgt + 10))
            fill.closeSubpath()
            ctx.fill(fill, with: .color(Theme.bg))
            // Dim at the horizon, brightest in the middle distance, fading again near the controls.
            let a = 0.07 + 0.36 * pow(s, 0.9) * (1 - 0.6 * s * s)
            ctx.stroke(line, with: .color(Theme.ink.opacity(a)), lineWidth: Theme.hairline)
        }
    }
}
