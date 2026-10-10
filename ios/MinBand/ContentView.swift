import SwiftUI
import UIKit

/// One screen, poster layout (docs/STYLE.md): the camera (or, in WIREFRAME, the stage scene; or,
/// while stopped, a contour terrain) full bleed; the `MINBAND` wordmark and one credits row
/// (LINK, TRACKS, ORIGIN, host) at the top; detections as corner ticks; hairline-separated text
/// controls at the bottom. No frame: content runs to the edges, bands are split by hairlines.
/// Everything secondary is behind `DETAILS` (collapsed by default, remembered).
struct ContentView: View {
    @StateObject private var pipeline = Pipeline()
    @AppStorage("host") private var host = "192.168.1.10"
    @AppStorage("details") private var details = false
    @State private var share: ShareItem?
    /// Record the H.264 baseline in the next run (`VideoBaseline`). Off by default and not
    /// remembered: three encoders cost battery and thermal headroom.
    @State private var h264 = false
    @State private var notice: String?
    @State private var noticeSerial = 0
    @State private var detectionsAt = Date.distantPast

    var body: some View {
        let hud = hudState
        ZStack {
            Theme.bg.ignoresSafeArea()
            stage
            VStack(spacing: 0) {
                // HUD band on a plain bg ground (top edge to its hairline): text never sits on the
                // camera and detection ticks never cross it.
                VStack(spacing: 0) {
                    TopHUD(state: hud, details: $details)
                    let lines = noticeLines(hud)
                    if !lines.isEmpty { NoticeStack(lines: lines).padding(.top, 18) }
                }
                .padding(.bottom, 20)
                .background(Theme.bg.ignoresSafeArea(edges: .top))
                Hairline()   // HUD | camera area
                // Centre notices sit in the "sky" above the standby terrain's ridge line. A ZStack,
                // not a Group: it must exist (and take the flexible height) even when empty.
                ZStack {
                    if pipeline.arUnsupported {
                        UnsupportedNotice()
                    } else if !hud.running && !details {
                        StandbyHint()
                    }
                }
                .padding(.top, 40)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                BottomBar(host: $host,
                          running: pipeline.running,
                          wireframe: pipeline.wireframe,
                          manualOrigin: pipeline.originSource == "manual",
                          h264: h264,
                          onH264: { h264.toggle() },
                          onStartStop: startStop,
                          onOriginHere: { pipeline.setOriginHere() },
                          onWireframe: { pipeline.setWireframe(!pipeline.wireframe) },
                          onShareLog: shareLog)
            }
            .padding(.top, 2)
        }
        .animation(Theme.fade, value: pipeline.running)
        .animation(Theme.fade, value: pipeline.wireframe)
        .animation(Theme.fade, value: pipeline.arUnsupported)
        .sheet(item: $share) { ActivityView(items: $0.urls) }
        .onReceive(pipeline.$detections) { _ in detectionsAt = .now }
    }

    // MARK: layers

    /// Full-screen layers in the AR view's coordinate space (the overlay maps boxes for it).
    private var stage: some View {
        GeometryReader { geo in
            ZStack(alignment: .topLeading) {
                ARViewContainer(pipeline: pipeline, wireframe: pipeline.wireframe)
                if !pipeline.running && !isDemo { ContourField().transition(.opacity) }
                TimelineView(.periodic(from: .now, by: 0.25)) { tl in
                    DetectionOverlay(boxes: boxes, marks: marks,
                                     stale: !isDemo && tl.date.timeIntervalSince(detectionsAt) > DetectionOverlay.staleAfter,
                                     showConfidence: details)
                }
            }
            .onAppear { pipeline.viewportSize = geo.size }
            .onChange(of: geo.size) { _, s in pipeline.viewportSize = s }
        }
        .ignoresSafeArea()
    }

    // MARK: actions

    private func startStop() {
        if pipeline.running { pipeline.stop() } else { pipeline.start(host: host, videoBaseline: h264) }
    }

    /// The newest ground-truth CSV, plus the H.264 baseline JSON of the same run if one was written.
    private func shareLog() {
        if pipeline.running { pipeline.flushLog() }
        guard let url = GroundTruthLog.latest() else { flash("no ground-truth log yet"); return }
        let baseline = VideoBaseline.reportURL(forLog: url)
        let hasBaseline = FileManager.default.fileExists(atPath: baseline.path)
        share = ShareItem(urls: hasBaseline ? [url, baseline] : [url])
    }

    /// Transient feedback in the notice stack (replaces a system alert).
    private func flash(_ text: String) {
        noticeSerial += 1
        let serial = noticeSerial
        notice = text
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { if noticeSerial == serial { notice = nil } }
    }

    // MARK: state

    private func noticeLines(_ hud: HUDState) -> [NoticeStack.Line] {
        var out: [NoticeStack.Line] = []
        if !pipeline.status.isEmpty, !pipeline.arUnsupported {
            out.append(.init(id: "status", text: pipeline.status, strong: true))
        }
        if let notice { out.append(.init(id: "notice", text: notice, strong: false)) }
        if !pipeline.baselineNote.isEmpty { out.append(.init(id: "baseline", text: pipeline.baselineNote, strong: false)) }
        if !hud.running, let e = hud.detectorError { out.append(.init(id: "detector", text: e, strong: false)) }
        return out
    }

    #if DEBUG
    private var isDemo: Bool { DemoMode.isOn }
    private var hudState: HUDState { DemoMode.isOn ? DemoMode.hud : HUDState(pipeline, host: host) }
    private var boxes: [OverlayBox] { DemoMode.isOn ? DemoMode.boxes : pipeline.detections }
    private var marks: [LiftMark] { DemoMode.isOn ? DemoMode.marks : pipeline.liftMarks }
    #else
    private var isDemo: Bool { false }
    private var hudState: HUDState { HUDState(pipeline, host: host) }
    private var boxes: [OverlayBox] { pipeline.detections }
    private var marks: [LiftMark] { pipeline.liftMarks }
    #endif
}

struct ShareItem: Identifiable {
    let urls: [URL]
    var id: String { urls.map(\.path).joined(separator: "\n") }
}

/// UIActivityViewController for sharing the ground-truth CSV and the H.264 baseline JSON
/// (AirDrop, Files, Mail...).
struct ActivityView: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        let vc = UIActivityViewController(activityItems: items, applicationActivities: nil)
        vc.overrideUserInterfaceStyle = .dark
        return vc
    }
    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}
