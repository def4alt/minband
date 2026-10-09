import SwiftUI

/// Bottom controls: one row of uppercase text buttons separated by hairlines (START/STOP,
/// ORIGIN HERE, WIREFRAME, SHARE LOG). Above the row, small toggles: `H.264` (also record the
/// H.264 baseline in the next run, see `VideoBaseline`) and `HOST` (reveals the inline
/// `HOST  192.168.1.10` field); both apply at START and are disabled while running. Active state
/// inverts (ink background, bg text). No fills otherwise, no rounded corners, no symbols.
struct BottomBar: View {
    @Binding var host: String
    let running: Bool
    let wireframe: Bool
    let manualOrigin: Bool
    let h264: Bool
    let onH264: () -> Void
    let onStartStop: () -> Void
    let onOriginHere: () -> Void
    let onWireframe: () -> Void
    let onShareLog: () -> Void

    @State private var hostOpen = false
    @FocusState private var hostFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Spacer()
                Button("H.264", action: onH264)
                    .buttonStyle(SmallButtonStyle(active: h264))
                    .disabled(running)
                    .accessibilityValue(h264 ? "on" : "off")
                    .accessibilityHint("Also record H.264 video at 720p, 480p and 360p in the next run to measure the video baseline")
                Button("HOST") { hostOpen.toggle() }
                    .buttonStyle(SmallButtonStyle(active: hostOpen))
                    .disabled(running)
                    .accessibilityHint("Edit the server address")
            }
            .padding(.horizontal, Theme.gutter - 8)
            .padding(.bottom, 10)
            panel
        }
        .animation(Theme.fade, value: hostOpen)
        .onChange(of: running) { _, r in if r { hostOpen = false } }
        .onChange(of: hostOpen) { _, open in
            if open { DispatchQueue.main.async { hostFocused = true } } else { hostFocused = false }
        }
    }

    /// The hairline-ruled band, full width. Plain `bg` ground (same tone as the page, so not a
    /// card) keeps the labels legible over the camera or the terrain.
    private var panel: some View {
        VStack(spacing: 0) {
            Hairline()
            if hostOpen && !running {
                hostRow
                    .transition(.opacity)
                Hairline()
            }
            SpreadRow {
                Button(running ? "STOP" : "START", action: onStartStop)
                    .buttonStyle(BarButtonStyle(active: running))
                Button("ORIGIN HERE", action: onOriginHere)
                    .buttonStyle(BarButtonStyle(active: manualOrigin, separator: true))
                    .disabled(!running)
                Button("WIREFRAME", action: onWireframe)
                    .buttonStyle(BarButtonStyle(active: wireframe, separator: true))
                    .accessibilityValue(wireframe ? "on" : "off")
                Button("SHARE LOG", action: onShareLog)
                    .buttonStyle(BarButtonStyle(active: false, separator: true))
            }
            .frame(height: 50)
        }
        // Runs to the bottom edge (under the home indicator); the screen edge closes the band.
        .background(Theme.bg.ignoresSafeArea(edges: .bottom))
    }

    private var hostRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text("HOST").labelStyle()
            TextField("", text: $host, prompt: Text("host[:port]").foregroundStyle(Theme.ink3))
                .font(Theme.mono(14))
                .foregroundStyle(Theme.ink)
                .tint(Theme.ink)
                .textFieldStyle(.plain)
                .keyboardType(.numbersAndPunctuation)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.done)
                .focused($hostFocused)
                .onSubmit { hostOpen = false }
                .accessibilityLabel("Server host")
                .overlay(alignment: .bottom) { Hairline(color: Theme.ink2).offset(y: 6) }
        }
        .padding(.horizontal, Theme.gutter)
        .frame(height: 48)
    }
}

/// Bar cell: uppercase text, inverted when `active` or pressed, `ink3` when disabled.
struct BarButtonStyle: ButtonStyle {
    var active: Bool
    /// Draw a vertical hairline on the leading edge (between cells of a row).
    var separator = false

    func makeBody(configuration: Configuration) -> some View {
        StyledLabel(configuration: configuration, active: active, separator: separator)
    }

    private struct StyledLabel: View {
        let configuration: ButtonStyleConfiguration
        let active: Bool
        let separator: Bool
        @Environment(\.isEnabled) private var isEnabled

        var body: some View {
            let inverted = isEnabled && (active || configuration.isPressed)
            configuration.label
                .labelStyle(10.5, color: !isEnabled ? Theme.ink3 : inverted ? Theme.bg : Theme.ink)
                .lineLimit(1)
                .fixedSize()
                .padding(.horizontal, 6)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                // Explicitly not into the safe area: the bar sits on the home-indicator margin.
                .background(inverted ? Theme.ink : Theme.bg.opacity(0.001), ignoresSafeAreaEdges: [])
                .overlay(alignment: .leading) { if separator { Hairline(axis: .vertical) } }
                .contentShape(Rectangle())
                .animation(configuration.isPressed ? nil : Theme.fade, value: inverted)
        }
    }
}

/// Small secondary toggle (`DETAILS`, `HOST`): uppercase text with a hairline underline,
/// inverted when active.
struct SmallButtonStyle: ButtonStyle {
    var active: Bool

    func makeBody(configuration: Configuration) -> some View {
        StyledLabel(configuration: configuration, active: active)
    }

    private struct StyledLabel: View {
        let configuration: ButtonStyleConfiguration
        let active: Bool
        @Environment(\.isEnabled) private var isEnabled

        var body: some View {
            let inverted = isEnabled && (active || configuration.isPressed)
            configuration.label
                .labelStyle(9.5, color: !isEnabled ? Theme.ink3 : inverted ? Theme.bg : Theme.ink2)
                .padding(.vertical, 5)
                .overlay(alignment: .bottom) {
                    Hairline(color: isEnabled && !inverted ? Theme.ink3 : .clear)
                }
                .padding(.horizontal, 8)
                .background(inverted ? Theme.ink : Theme.bg.opacity(0.001), ignoresSafeAreaEdges: [])
                // Comfortable touch target around small type.
                .padding(.vertical, 6)
                .contentShape(Rectangle())
                .animation(configuration.isPressed ? nil : Theme.fade, value: inverted)
        }
    }
}

/// Lays children out in one row: each gets its ideal width plus an equal share of the leftover,
/// so labels of different lengths ("START", "ORIGIN HERE") keep even padding instead of
/// truncating in equal-width cells. Children narrower than ideal are scaled down proportionally.
struct SpreadRow: Layout {
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let sizes = subviews.map { $0.sizeThatFits(.unspecified) }
        return CGSize(width: proposal.width ?? sizes.reduce(0) { $0 + $1.width },
                      height: proposal.height ?? sizes.map(\.height).max() ?? 0)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard !subviews.isEmpty else { return }
        let ideal = subviews.map { $0.sizeThatFits(.unspecified).width }
        let total = ideal.reduce(0, +)
        let scale = total > bounds.width ? bounds.width / max(total, 1) : 1
        let extra = max(0, bounds.width - total) / CGFloat(subviews.count)
        var x = bounds.minX
        for (i, s) in subviews.enumerated() {
            let w = ideal[i] * scale + extra
            s.place(at: CGPoint(x: x, y: bounds.minY), anchor: .topLeading,
                    proposal: ProposedViewSize(width: w, height: bounds.height))
            x += w
        }
    }
}
