import SwiftUI

/// 2D detections as corner ticks (four 10 pt hairline brackets, not rectangles) labelled
/// `PERSON · 03` (class · track id; `· 0.82` confidence is added under DETAILS) in monospace
/// above the top-left corner.
/// A box that fed a confirmed track is solid pure white (difference blend) and gets a `+` where its lifted 3D point
/// projects back into the view (a person's feet, an object's centre). Tentative boxes (no track
/// id yet) and stale ones (no detector update for `staleAfter`) are dotted and dimmer.
struct DetectionOverlay: View {
    let boxes: [OverlayBox]
    let marks: [LiftMark]
    let stale: Bool
    var showConfidence = false

    static let staleAfter: TimeInterval = 0.5
    static let tick: CGFloat = 10
    static let markArm: CGFloat = 5

    /// Overlay strokes are pure white at 1.5 pt with a dark halo underneath, so they stay
    /// visible over bright and dark parts of the camera feed alike.
    static let strokeWidth: CGFloat = 1.5

    var body: some View {
        Canvas { ctx, size in
            var strokes = ctx
            strokes.addFilter(.shadow(color: .black.opacity(0.9), radius: 1.5, x: 0, y: 0))
            for b in boxes {
                let r = CGRect(x: b.rect.minX * size.width, y: b.rect.minY * size.height,
                               width: b.rect.width * size.width, height: b.rect.height * size.height)
                guard r.width >= 1, r.height >= 1 else { continue }
                let firm = b.trackId != nil && !stale
                let color = Color.white.opacity(firm ? 1 : 0.7)
                let style = firm
                    ? StrokeStyle(lineWidth: Self.strokeWidth, lineCap: .square)
                    : StrokeStyle(lineWidth: Self.strokeWidth, lineCap: .butt, dash: [2, 3])
                strokes.stroke(Self.cornerTicks(r, length: Self.tick), with: .color(color), style: style)

                // Label: white with a dark halo so it reads over bright and dark areas alike.
                let text = ctx.resolve(Text(Self.label(b, confidence: showConfidence)).font(Theme.mono(10).weight(.medium)).tracking(0.5).foregroundStyle(Color.white.opacity(firm ? 1 : 0.8)))
                let ts = text.measure(in: size)
                let x = min(max(0, r.minX), max(0, size.width - ts.width))
                let y = max(0, r.minY - ts.height - 3)
                ctx.drawLayer { layer in
                    layer.addFilter(.shadow(color: .black.opacity(0.9), radius: 1.5, x: 0, y: 0))
                    layer.draw(text, at: CGPoint(x: x, y: y), anchor: .topLeading)
                }
            }
            var plus = Path()
            let a = Self.markArm
            for m in marks {
                let p = CGPoint(x: m.point.x * size.width, y: m.point.y * size.height)
                guard p.x > -a, p.y > -a, p.x < size.width + a, p.y < size.height + a else { continue }
                plus.move(to: CGPoint(x: p.x - a, y: p.y)); plus.addLine(to: CGPoint(x: p.x + a, y: p.y))
                plus.move(to: CGPoint(x: p.x, y: p.y - a)); plus.addLine(to: CGPoint(x: p.x, y: p.y + a))
            }
            strokes.stroke(plus, with: .color(Color.white.opacity(stale ? 0.7 : 1)), lineWidth: Self.strokeWidth)
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    /// Four L-shaped brackets, one per corner, `length` long (shorter for tiny boxes).
    static func cornerTicks(_ r: CGRect, length: CGFloat) -> Path {
        let l = min(length, r.width / 2, r.height / 2)
        var p = Path()
        func corner(_ c: CGPoint, _ dx: CGFloat, _ dy: CGFloat) {
            p.move(to: CGPoint(x: c.x + dx * l, y: c.y))
            p.addLine(to: c)
            p.addLine(to: CGPoint(x: c.x, y: c.y + dy * l))
        }
        corner(CGPoint(x: r.minX, y: r.minY), 1, 1)
        corner(CGPoint(x: r.maxX, y: r.minY), -1, 1)
        corner(CGPoint(x: r.minX, y: r.maxY), 1, -1)
        corner(CGPoint(x: r.maxX, y: r.maxY), -1, -1)
        return p
    }

    /// `PERSON · 03`, or just `PERSON` until the box feeds a confirmed track; with `confidence`,
    /// `PERSON · 03 · 0.82`.
    static func label(_ b: OverlayBox, confidence: Bool) -> String {
        var parts = [TrackedClass.name(b.classId).uppercased()]
        if let id = b.trackId { parts.append(String(format: "%02u", id)) }
        if confidence { parts.append(String(format: "%.2f", b.conf)) }
        return parts.joined(separator: " · ")
    }
}

#if DEBUG
/// `-MinBandDemo` launch argument (DEBUG builds only): fills the HUD and the overlay with fixed
/// sample values so the chrome can be checked in the simulator, which has no ARKit tracking.
enum DemoMode {
    static let isOn = ProcessInfo.processInfo.arguments.contains("-MinBandDemo")

    static let boxes: [OverlayBox] = [
        OverlayBox(id: 0, rect: CGRect(x: 0.16, y: 0.33, width: 0.30, height: 0.40), classId: TrackedClass.person, conf: 0.82, trackId: 3),
        OverlayBox(id: 1, rect: CGRect(x: 0.58, y: 0.52, width: 0.24, height: 0.15), classId: TrackedClass.chair, conf: 0.47, trackId: nil),
        OverlayBox(id: 2, rect: CGRect(x: 0.62, y: 0.37, width: 0.07, height: 0.06), classId: TrackedClass.cup, conf: 0.66, trackId: 7),
    ]
    static let marks: [LiftMark] = [
        LiftMark(id: 0, trackId: 3, point: CGPoint(x: 0.31, y: 0.715)),
        LiftMark(id: 2, trackId: 7, point: CGPoint(x: 0.655, y: 0.40)),
    ]

    static var hud: HUDState {
        var s = HUDState()
        s.running = true; s.link = .up; s.host = "192.168.1.10"; s.originLocked = true; s.originSource = "marker"
        s.trackCount = 3; s.bytesPerSec = 141; s.wireBytesPerSec = 150; s.seq = 412; s.thetaScale = 1
        s.fps = 60; s.detectHz = 12; s.detectorStatus = "model yolov8n"; s.depthMode = "lidar"
        s.featurePoints = 312; s.meshAnchors = 14; s.meshSupported = true; s.deviceId = DeviceIdentity.id
        return s
    }
}
#endif
