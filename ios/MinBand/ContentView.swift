import SwiftUI

/// Camera preview with detection overlays, link status, and the server address field.
struct ContentView: View {
    @StateObject private var pipeline = Pipeline()
    @State private var host = UserDefaults.standard.string(forKey: "host") ?? "192.168.1.10"

    var body: some View {
        ZStack(alignment: .bottom) {
            ARViewContainer(pipeline: pipeline).ignoresSafeArea()
            VStack(spacing: 8) {
                Text(pipeline.originLocked ? "origin locked" : "show the marker").font(.caption)
                Text("\(pipeline.trackCount) tracks  \(pipeline.bytesPerSec) B/s  seq \(pipeline.seq)  θ×\(pipeline.thetaScale, specifier: "%.2f")")
                    .font(.caption.monospaced())
                HStack {
                    TextField("server host", text: $host).textFieldStyle(.roundedBorder)
                    Button(pipeline.running ? "Stop" : "Start") {
                        UserDefaults.standard.set(host, forKey: "host")
                        pipeline.running ? pipeline.stop() : pipeline.start(host: host)
                    }.buttonStyle(.borderedProminent)
                }
            }
            .padding().background(.ultraThinMaterial)
        }
    }
}
