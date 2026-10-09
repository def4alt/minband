import ARKit
import SceneKit
import SwiftUI

/// Camera preview (ARSCNView on the pipeline's session) plus RGB axes at the locked origin
/// (X red, Y green, Z blue, 20 cm) so the marker frame convention can be checked by eye.
struct ARViewContainer: UIViewRepresentable {
    let pipeline: Pipeline
    /// Changes whenever the origin is (re)locked so SwiftUI calls `updateUIView`.
    let originKey: String

    func makeUIView(context: Context) -> ARSCNView {
        let v = ARSCNView()
        v.session = pipeline.session
        v.automaticallyUpdatesLighting = true
        v.scene.rootNode.addChildNode(context.coordinator.axes)
        return v
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {
        let axes = context.coordinator.axes
        axes.isHidden = !Origin.shared.isLocked
        if Origin.shared.isLocked { axes.simdTransform = Origin.shared.markerTransform }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator {
        let axes: SCNNode = {
            let root = SCNNode()
            func axis(_ color: UIColor, _ dir: SIMD3<Float>) -> SCNNode {
                let g = SCNCylinder(radius: 0.006, height: 0.2)
                g.firstMaterial?.diffuse.contents = color
                g.firstMaterial?.lightingModel = .constant
                let n = SCNNode(geometry: g)
                // Cylinder is along +Y; rotate it onto `dir` and shift so it starts at the origin.
                n.simdOrientation = simd_quatf(from: SIMD3(0, 1, 0), to: dir)
                n.simdPosition = dir * 0.1
                return n
            }
            root.addChildNode(axis(.systemRed, SIMD3(1, 0, 0)))
            root.addChildNode(axis(.systemGreen, SIMD3(0, 1, 0)))
            root.addChildNode(axis(.systemBlue, SIMD3(0, 0, 1)))
            root.isHidden = true
            return root
        }()
    }
}
