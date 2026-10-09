import SwiftUI
import UIKit

/// Visual tokens from docs/STYLE.md. Monochrome, near-black, hairlines, thin type. The product is
/// dark by design: there is no light variant of any token.
enum Theme {
    // MARK: colour (STYLE.md "Tokens")

    static let bgHex: UInt32 = 0x070809     // scene background, never pure black
    static let inkHex: UInt32 = 0xE9ECEF    // primary lines, values, titles
    static let ink2Hex: UInt32 = 0x9AA3AD   // secondary text, labels
    static let ink3Hex: UInt32 = 0x4A525B   // hairlines, grid, disabled
    static let ink4Hex: UInt32 = 0x1A1E23   // faint grid, far contours

    static let bg = Color(hex: bgHex)
    static let ink = Color(hex: inkHex)
    static let ink2 = Color(hex: ink2Hex)
    static let ink3 = Color(hex: ink3Hex)
    static let ink4 = Color(hex: ink4Hex)

    /// UIKit / SceneKit variants of the same tokens.
    enum UI {
        static let bg = UIColor(hex: Theme.bgHex)
        static let ink = UIColor(hex: Theme.inkHex)
        static let ink2 = UIColor(hex: Theme.ink2Hex)
        static let ink3 = UIColor(hex: Theme.ink3Hex)
    }

    // MARK: geometry

    /// Every line in the product.
    static let hairline: CGFloat = 1
    /// Text inset from the screen edge. No frame around the screen: content runs to the edges and
    /// panels are separated by hairlines only (STYLE.md "Surfaces"; "Restraint": 24 px padding).
    static let gutter: CGFloat = 24

    // MARK: type (STYLE.md "Type")

    /// Labels: uppercase, 0.18 em tracking, 10 to 11 pt, `ink2`.
    static let labelSize: CGFloat = 10
    static let labelTracking: CGFloat = labelSize * 0.18
    static func label(_ size: CGFloat = labelSize) -> Font { .system(size: size, weight: .regular) }

    /// Values: `ink`, 12 to 14 pt, weight 400, tabular digits.
    static let valueSize: CGFloat = 13
    static func value(_ size: CGFloat = valueSize) -> Font { .system(size: size, weight: .regular).monospacedDigit() }

    /// The primary readout (LINK, TRACKS): one size step and one tone step above everything else.
    static let primary = Font.system(size: 20, weight: .regular).monospacedDigit()
    static let primaryWord = Font.system(size: 20, weight: .light)

    /// Numbers only: SF Mono.
    static func mono(_ size: CGFloat = 12) -> Font { .system(size: size, weight: .regular, design: .monospaced) }

    /// Title `MINBAND`: weight 200 to 300, 0.3 em tracking.
    static let titleSize: CGFloat = 20
    static let title = Font.system(size: titleSize, weight: .light)
    static let titleTracking: CGFloat = titleSize * 0.3

    // MARK: motion (STYLE.md "Motion: slow")

    static let fade = Animation.easeInOut(duration: 0.4)
    /// Stale / alert blink: 1 Hz between 40 and 100 % opacity.
    static let blinkLow: Double = 0.4
}

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: opacity)
    }
}

extension UIColor {
    convenience init(hex: UInt32, alpha: CGFloat = 1) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255,
                  green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255,
                  alpha: alpha)
    }
}

extension View {
    /// Letter-spaced, dim: the `LABEL` half of a `LABEL  Value` pair. Pass uppercase text (not
    /// `textCase`, which would turn `θ` into `Θ`).
    func labelStyle(_ size: CGFloat = Theme.labelSize, color: Color = Theme.ink2) -> some View {
        font(Theme.label(size)).tracking(size * 0.18).foregroundStyle(color)
    }

    /// 1 Hz, 40 to 100 % opacity blink. Only for a lost link (STYLE.md "Restraint").
    func blinking(_ on: Bool = true) -> some View { modifier(Blink(active: on)) }
}

private struct Blink: ViewModifier {
    let active: Bool
    func body(content: Content) -> some View {
        if active {
            TimelineView(.periodic(from: .now, by: 0.5)) { ctx in
                let phase = Int(ctx.date.timeIntervalSinceReferenceDate * 2) % 2
                content.opacity(phase == 0 ? 1 : Theme.blinkLow)
                    .animation(.easeInOut(duration: 0.45), value: phase)
            }
        } else {
            content
        }
    }
}
