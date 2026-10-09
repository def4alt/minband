import SwiftUI

/// Everything the HUD shows, copied out of `Pipeline` once per render so the views below are
/// plain values (and can be fed demo numbers in DEBUG builds).
struct HUDState {
    var running = false
    var wireframe = false
    var link: LinkState = .off
    var host = ""
    var originLocked = false
    var originSource = "none"
    var trackCount = 0
    var bytesPerSec = 0
    var wireBytesPerSec = 0
    var seq: UInt32 = 0
    var thetaScale = 1.0
    var fps = 0.0
    var detectHz = 0.0
    var detectorStatus = ""
    var depthMode = "-"
    var featurePoints = 0
    var meshAnchors = 0
    var meshSupported = false
    var deviceId: UInt32 = 0

    init() {}

    init(_ p: Pipeline, host: String) {
        running = p.running; wireframe = p.wireframe; link = p.link; self.host = host
        originLocked = p.originLocked; originSource = p.originSource
        trackCount = p.trackCount; bytesPerSec = p.bytesPerSec; wireBytesPerSec = p.wireBytesPerSec
        seq = p.seq; thetaScale = p.thetaScale; fps = p.fps; detectHz = p.detectHz
        detectorStatus = p.detectorStatus; depthMode = p.depthMode
        featurePoints = p.featurePoints; meshAnchors = p.meshAnchors
        meshSupported = Pipeline.supportsMesh; deviceId = DeviceIdentity.id
    }

    // MARK: formatted values ("—" while stopped, so the rows read as standby, not as zeros)

    static let dash = "—"

    /// kbps with one decimal: measured wire rate, UDP/IP headers included.
    var kbps: String { String(format: "%.1f", Double(wireBytesPerSec) * 8 / 1000) }
    var tracks: String { running ? "\(trackCount)" : Self.dash }
    var origin: String { running ? (originLocked ? "locked" : "searching") : Self.dash }
    var hostText: String { host.trimmingCharacters(in: .whitespaces).isEmpty ? "no host" : host }

    var model: String {
        if detectorStatus.hasPrefix("model ") { return String(detectorStatus.dropFirst(6)) }
        if detectorStatus.hasPrefix("no detector") { return "none" }
        return detectorStatus.isEmpty ? Self.dash : detectorStatus
    }
    /// Full detector error, shown as a notice while stopped.
    var detectorError: String? {
        guard detectorStatus.hasPrefix("no detector") else { return nil }
        let detail = detectorStatus.drop(while: { $0 != ":" }).dropFirst().trimmingCharacters(in: .whitespaces)
        return detail.isEmpty ? "no detector" : "no detector · \(detail)"
    }

    /// Secondary numbers, only under DETAILS (STYLE.md "Restraint: progressive disclosure").
    var details: [(String, String)] {
        let r = running
        func v(_ s: @autoclosure () -> String) -> String { r ? s() : Self.dash }
        return [
            ("SEQ", v("\(seq)")), ("θ", v(String(format: "×%.2f", thetaScale))),
            ("FPS", v(String(format: "%.0f", fps))), ("DET", v(String(format: "%.0f Hz", detectHz))),
            ("WIRE", v("\(wireBytesPerSec) B/s")), ("CORE", v("\(bytesPerSec) B/s")),
            ("DEPTH", depthMode == "-" ? Self.dash : depthMode), ("MODEL", model),
            ("POINTS", v("\(featurePoints)")), ("MESH", !meshSupported ? "n/a" : r && wireframe ? "\(meshAnchors)" : "off"),
            ("SOURCE", originLocked ? originSource : Self.dash), ("DEV", String(deviceId)),
        ]
    }
}

/// Top of the screen: the `MINBAND` wordmark, a `DETAILS` toggle, and one credits row of three
/// pairs. LINK and TRACKS are the primary readout; ORIGIN is a step quieter in size and tone;
/// the server host sits under LINK in the quietest tone. Long press anywhere on it also toggles
/// DETAILS.
struct TopHUD: View {
    let state: HUDState
    @Binding var details: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(alignment: .center) {
                Text("MINBAND")
                    .font(Theme.title).tracking(Theme.titleTracking)
                    .foregroundStyle(Theme.ink)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 12)
                Button("DETAILS") { details.toggle() }
                    .buttonStyle(SmallButtonStyle(active: details))
                    .accessibilityValue(details ? "shown" : "hidden")
                    .padding(.trailing, -8)   // text, not its touch padding, on the gutter
            }
            HStack(alignment: .top, spacing: 0) {
                column("LINK") {
                    linkValue
                    Text(verbatim: "to \(state.hostText)")
                        .font(Theme.mono(11)).foregroundStyle(Theme.ink3)
                        .lineLimit(1).truncationMode(.middle)
                }
                column("TRACKS") {
                    Text(state.tracks).font(Theme.primary).foregroundStyle(Theme.ink)
                }
                column("ORIGIN") {
                    Text(state.origin).font(Theme.value()).foregroundStyle(Theme.ink2)
                        .padding(.top, 3)   // sit on the primary values' baseline band
                }
            }
            if details {
                DetailsGrid(pairs: state.details)
                    .transition(.opacity)
            }
        }
        .padding(.horizontal, Theme.gutter)
        .padding(.top, 6)
        .contentShape(Rectangle())
        .onLongPressGesture { details.toggle() }
        .animation(Theme.fade, value: details)
    }

    @ViewBuilder private var linkValue: some View {
        switch state.link {
        case .off:
            Text(HUDState.dash).font(Theme.primary).foregroundStyle(Theme.ink)
        case .waiting:
            Text("waiting").font(Theme.primaryWord).foregroundStyle(Theme.ink2)
        case .lost:
            // The only thing in the app that blinks (STYLE.md "Restraint").
            Text("lost").font(Theme.primaryWord).foregroundStyle(Theme.ink).blinking()
        case .up:
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(state.kbps).font(Theme.primary).foregroundStyle(Theme.ink)
                Text("kbps").font(Theme.value(11)).foregroundStyle(Theme.ink2)
            }
        }
    }

    private func column<V: View>(_ label: String, @ViewBuilder _ value: () -> V) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).labelStyle()
            value()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// Secondary numbers in two columns of small `LABEL  value` pairs under a hairline.
struct DetailsGrid: View {
    let pairs: [(String, String)]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Hairline()
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 10, verticalSpacing: 9) {
                ForEach(Array(stride(from: 0, to: pairs.count, by: 2)), id: \.self) { i in
                    GridRow {
                        cell(pairs[i])
                        if i + 1 < pairs.count { cell(pairs[i + 1]) }
                    }
                }
            }
        }
        .lineLimit(1)
        .minimumScaleFactor(0.8)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private func cell(_ p: (String, String)) -> some View {
        Text(p.0).labelStyle(9, color: Theme.ink3)
        Text(p.1).font(Theme.value(11)).foregroundStyle(Theme.ink2)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Status lines under the HUD (tracking state, errors, transient feedback): uppercase,
/// letter-spaced, quiet. Nothing here blinks.
struct NoticeStack: View {
    struct Line: Identifiable { let id: String; let text: String; let strong: Bool }
    let lines: [Line]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(lines) { l in
                Text(l.text.uppercased())
                    .labelStyle(color: l.strong ? Theme.ink : Theme.ink2)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, Theme.gutter)
        .animation(Theme.fade, value: lines.map(\.id))
    }
}

/// Centre notice when this device (or the simulator) cannot run ARKit world tracking.
struct UnsupportedNotice: View {
    var body: some View {
        VStack(spacing: 12) {
            Text("ARKIT UNAVAILABLE").labelStyle(11, color: Theme.ink)
            Text("World tracking is not supported on this device.\nThe HUD runs; the pipeline does not.")
                .font(.system(size: 13, weight: .light))
                .foregroundStyle(Theme.ink2)
                .multilineTextAlignment(.center)
                .lineSpacing(4)
        }
        .padding(.vertical, 16)
        .padding(.horizontal, 12)
        .background(TerrainKnockout())
        .frame(maxWidth: .infinity)
        .transition(.opacity)
    }
}

/// Centre hint while stopped.
struct StandbyHint: View {
    var body: some View {
        VStack(spacing: 10) {
            Text("POINT AT THE ORIGIN MARKER").labelStyle(color: Theme.ink2)
            Text("THEN PRESS START").labelStyle(9, color: Theme.ink3)
        }
        .frame(maxWidth: .infinity)
        .transition(.opacity)
    }
}

/// Soft `bg` patch behind centred text so it never sits on contour lines. Not a panel: no edge,
/// no tone of its own.
struct TerrainKnockout: View {
    var body: some View { Theme.bg.blur(radius: 12) }
}
