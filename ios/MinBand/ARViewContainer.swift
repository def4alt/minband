import ARKit
import SwiftUI

struct ARViewContainer: UIViewRepresentable {
    let pipeline: Pipeline
    func makeUIView(context: Context) -> ARSCNView {
        let v = ARSCNView(); v.session = pipeline.session; v.automaticallyUpdatesLighting = true; return v
    }
    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}
