import SwiftUI
import UIKit

/// Camera preview with detection overlays, link status, and the server address field.
struct ContentView: View {
    @StateObject private var pipeline = Pipeline()
    @AppStorage("host") private var host = "192.168.1.10"
    @State private var share: ShareItem?
    @State private var shareError = false

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .topLeading) {
                ARViewContainer(pipeline: pipeline, originKey: "\(pipeline.originSource)-\(pipeline.originLocked)")
                DetectionOverlay(boxes: pipeline.detections, size: geo.size)
            }
            .onAppear { pipeline.viewportSize = geo.size }
            .onChange(of: geo.size) { _, s in pipeline.viewportSize = s }
        }
        .ignoresSafeArea()
        .overlay(alignment: .top) { statusBar }
        .overlay(alignment: .bottom) { controls }
        .sheet(item: $share) { ActivityView(items: [$0.url]) }
        .alert("No ground-truth log yet", isPresented: $shareError) { Button("OK", role: .cancel) {} }
    }

    private var statusBar: some View {
        VStack(spacing: 2) {
            Text(originText).font(.caption.bold())
            Text("\(pipeline.detectorStatus) · depth \(pipeline.depthMode)").font(.caption2)
            if !pipeline.status.isEmpty { Text(pipeline.status).font(.caption2).foregroundStyle(.yellow) }
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 8))
        .padding(.top, 4)
    }

    private var controls: some View {
        VStack(spacing: 8) {
            Text(statsText).font(.caption.monospaced()).lineLimit(2).minimumScaleFactor(0.7)
            HStack {
                TextField("server host[:port]", text: $host)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.numbersAndPunctuation)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .disabled(pipeline.running)
                Button(pipeline.running ? "Stop" : "Start") {
                    pipeline.running ? pipeline.stop() : pipeline.start(host: host)
                }.buttonStyle(.borderedProminent)
            }
            HStack {
                Button("origin: set here") { pipeline.setOriginHere() }
                    .buttonStyle(.bordered).disabled(!pipeline.running)
                Spacer()
                Button("share log") {
                    if pipeline.running { pipeline.flushLog() }
                    if let url = GroundTruthLog.latest() { share = ShareItem(url: url) } else { shareError = true }
                }.buttonStyle(.bordered)
            }
        }
        .padding()
        .background(.ultraThinMaterial)
    }

    private var originText: String {
        switch pipeline.originSource {
        case "marker": return "origin: marker"
        case "manual": return "origin: manual (set here)"
        default: return pipeline.running ? "show the marker" : "stopped"
        }
    }

    private var statsText: String {
        String(format: "%d tracks  %d B/s (wire %d)  seq %u  θ×%.2f  %.0f fps  det %.0f Hz",
               pipeline.trackCount, pipeline.bytesPerSec, pipeline.wireBytesPerSec, pipeline.seq,
               pipeline.thetaScale, pipeline.fps, pipeline.detectHz)
    }
}

/// 2D boxes with class label, confidence and track id (if the detection fed a confirmed track).
struct DetectionOverlay: View {
    let boxes: [OverlayBox]
    let size: CGSize

    var body: some View {
        ZStack(alignment: .topLeading) {
            ForEach(boxes) { b in
                let r = CGRect(x: b.rect.minX * size.width, y: b.rect.minY * size.height,
                               width: b.rect.width * size.width, height: b.rect.height * size.height)
                let color = DetectionOverlay.color(b.classId)
                // Absolute placement: offsets inside a top-leading ZStack.
                Rectangle()
                    .stroke(color, lineWidth: b.trackId == nil ? 1.5 : 3)
                    .frame(width: max(1, r.width), height: max(1, r.height))
                    .offset(x: r.minX, y: r.minY)
                Text(label(b))
                    .font(.caption2.monospaced().bold())
                    .padding(.horizontal, 3)
                    .background(color.opacity(0.8))
                    .foregroundStyle(.black)
                    .fixedSize()
                    .offset(x: r.minX, y: max(0, r.minY - 16))
            }
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
        .allowsHitTesting(false)
    }

    private func label(_ b: OverlayBox) -> String {
        let id = b.trackId.map { " #\($0)" } ?? ""
        return "\(TrackedClass.name(b.classId))\(id) \(Int((b.conf * 100).rounded()))%"
    }

    static func color(_ cls: UInt8) -> Color {
        switch cls {
        case TrackedClass.person: return .green
        case TrackedClass.chair, TrackedClass.tv, TrackedClass.laptop: return .orange
        default: return .cyan
        }
    }
}

struct ShareItem: Identifiable {
    let url: URL
    var id: String { url.path }
}

/// UIActivityViewController for sharing the ground-truth CSV (AirDrop, Files, Mail...).
struct ActivityView: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}
