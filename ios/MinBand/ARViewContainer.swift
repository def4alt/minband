import ARKit
import SceneKit
import SwiftUI

/// ARSCNView on the pipeline's session, plus the scene content drawn over it:
///
/// - Marker axes at the locked origin, both modes: X/Y/Z as 1 px white lines of 0.3 m with tiny
///   `X` `Y` `Z` labels (billboards), always on top.
/// - WIREFRAME stage mode: the camera is hidden behind a `bg`-coloured dome that follows the
///   camera, the LiDAR scene-reconstruction mesh (`ARMeshAnchor`) is drawn as additive white
///   wires, and ARKit's feature points accumulate into a monochrome point cloud (fresh points
///   bright, older ones fading). Non-LiDAR devices get the points only. ARKit's own
///   `.showFeaturePoints` debug option is not used because it draws yellow.
///
/// The camera feed stays the default (colour) mode.
struct ARViewContainer: UIViewRepresentable {
    let pipeline: Pipeline
    let wireframe: Bool

    func makeUIView(context: Context) -> ARSCNView {
        let v = ARSCNView(frame: .zero)
        v.session = pipeline.session
        v.delegate = context.coordinator
        v.backgroundColor = Theme.UI.bg
        v.antialiasingMode = .multisampling4X
        v.automaticallyUpdatesLighting = false
        v.rendersCameraGrain = false
        v.scene.rootNode.addChildNode(context.coordinator.axes)
        v.scene.rootNode.addChildNode(context.coordinator.cloud)
        context.coordinator.apply(wireframe: wireframe)
        return v
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {
        context.coordinator.apply(wireframe: wireframe)
    }

    func makeCoordinator() -> StageRenderer { StageRenderer(session: pipeline.session) }
}

/// ARSCNViewDelegate for the stage content. `apply(wireframe:)` runs on main; the renderer
/// callbacks run on SceneKit's render thread. The shared flag and mesh-node table are locked.
final class StageRenderer: NSObject, ARSCNViewDelegate {
    let axes = MarkerAxes.make()
    let cloud: SCNNode = {
        let n = SCNNode()
        n.name = "feature-cloud"
        n.isHidden = true
        return n
    }()
    private let dome = StageRenderer.makeDome()
    private let meshMaterial = StageRenderer.makeMeshMaterial()
    private let pointMaterial = StageRenderer.makePointMaterial()
    private weak var session: ARSession?

    private let lock = NSLock()
    private var _wireframe = false
    private var meshNodes: [UUID: SCNNode] = [:]

    // Render thread only.
    private var points: [UInt64: (pos: SIMD3<Float>, seen: Int)] = [:]
    private var cloudGeneration = 0
    private var lastCloudBuild: TimeInterval = 0

    static let cloudInterval: TimeInterval = 0.1   // rebuild the point cloud at 10 Hz
    static let cloudCapacity = 6000
    static let cloudFade = 40                      // rebuilds until a point is at its dimmest

    init(session: ARSession) {
        self.session = session
        super.init()
    }

    private var wireframe: Bool { lock.withLock { _wireframe } }

    /// Main thread. Cheap when nothing changed.
    func apply(wireframe on: Bool) {
        let nodes: [SCNNode]? = lock.withLock {
            guard on != _wireframe else { return nil }
            _wireframe = on
            return Array(meshNodes.values)
        }
        guard let nodes else { return }
        dome.isHidden = !on
        cloud.isHidden = !on
        for n in nodes { n.isHidden = !on }
    }

    // MARK: SCNSceneRendererDelegate

    func renderer(_ renderer: SCNSceneRenderer, updateAtTime time: TimeInterval) {
        // Follow marker re-locks every frame (Origin is thread-safe).
        let origin = Origin.shared
        if origin.isLocked {
            axes.simdTransform = origin.markerTransform
            axes.isHidden = false
        } else {
            axes.isHidden = true
        }

        guard wireframe else {
            if !points.isEmpty { points.removeAll(); cloud.geometry = nil }
            return
        }
        if let pov = renderer.pointOfView, dome.parent !== pov { pov.addChildNode(dome) }
        if time - lastCloudBuild >= StageRenderer.cloudInterval {
            lastCloudBuild = time
            updateCloud()
        }
    }

    // MARK: ARSCNViewDelegate (mesh anchors)

    func renderer(_ renderer: SCNSceneRenderer, didAdd node: SCNNode, for anchor: ARAnchor) {
        guard let mesh = anchor as? ARMeshAnchor else { return }
        let child = SCNNode(geometry: StageRenderer.geometry(mesh.geometry, material: meshMaterial))
        child.name = "mesh"
        let on = lock.withLock { meshNodes[anchor.identifier] = child; return _wireframe }
        child.isHidden = !on
        node.addChildNode(child)
    }

    func renderer(_ renderer: SCNSceneRenderer, didUpdate node: SCNNode, for anchor: ARAnchor) {
        guard let mesh = anchor as? ARMeshAnchor,
              let child = lock.withLock({ meshNodes[anchor.identifier] }) else { return }
        child.geometry = StageRenderer.geometry(mesh.geometry, material: meshMaterial)
    }

    func renderer(_ renderer: SCNSceneRenderer, didRemove node: SCNNode, for anchor: ARAnchor) {
        guard anchor is ARMeshAnchor else { return }
        lock.withLock { _ = meshNodes.removeValue(forKey: anchor.identifier) }
    }

    // MARK: feature-point cloud

    private func updateCloud() {
        guard let pc = session?.currentFrame?.rawFeaturePoints else { return }
        cloudGeneration += 1
        let g = cloudGeneration
        for (id, p) in zip(pc.identifiers, pc.points) { points[id] = (p, g) }
        if points.count > StageRenderer.cloudCapacity {
            // Keep the most recently seen two thirds.
            let keep = points.sorted { $0.value.seen > $1.value.seen }.prefix(StageRenderer.cloudCapacity * 2 / 3)
            points = Dictionary(uniqueKeysWithValues: keep.map { ($0.key, $0.value) })
        }
        var verts: [SIMD3<Float>] = []
        var colors: [SIMD4<Float>] = []
        verts.reserveCapacity(points.count)
        colors.reserveCapacity(points.count)
        for e in points.values {
            let age = Float(g - e.seen) / Float(StageRenderer.cloudFade)
            let b = max(0.18, 1 - age) * 0.9
            verts.append(e.pos)
            colors.append(SIMD4(b, b, b, 1))
        }
        guard !verts.isEmpty else { cloud.geometry = nil; return }
        let vStride = MemoryLayout<SIMD3<Float>>.stride
        let cStride = MemoryLayout<SIMD4<Float>>.stride
        let vs = SCNGeometrySource(data: verts.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: .vertex,
                                   vectorCount: verts.count, usesFloatComponents: true, componentsPerVector: 3,
                                   bytesPerComponent: MemoryLayout<Float>.size, dataOffset: 0, dataStride: vStride)
        let cs = SCNGeometrySource(data: colors.withUnsafeBufferPointer { Data(buffer: $0) }, semantic: .color,
                                   vectorCount: colors.count, usesFloatComponents: true, componentsPerVector: 4,
                                   bytesPerComponent: MemoryLayout<Float>.size, dataOffset: 0, dataStride: cStride)
        let el = SCNGeometryElement(indices: Array(0..<UInt32(verts.count)), primitiveType: .point)
        el.pointSize = 0.006
        el.minimumPointScreenSpaceRadius = 1
        el.maximumPointScreenSpaceRadius = 2.5
        let geo = SCNGeometry(sources: [vs, cs], elements: [el])
        geo.materials = [pointMaterial]
        cloud.geometry = geo
    }

    // MARK: geometry and materials

    /// ARMeshGeometry -> SCNGeometry (vertices and triangle indices copied out of ARKit's buffers).
    static func geometry(_ g: ARMeshGeometry, material: SCNMaterial) -> SCNGeometry {
        let v = g.vertices
        let vData = Data(bytes: v.buffer.contents().advanced(by: v.offset), count: v.stride * v.count)
        let vs = SCNGeometrySource(data: vData, semantic: .vertex, vectorCount: v.count, usesFloatComponents: true,
                                   componentsPerVector: 3, bytesPerComponent: MemoryLayout<Float>.size,
                                   dataOffset: 0, dataStride: v.stride)
        let f = g.faces
        let fData = Data(bytes: f.buffer.contents(), count: f.count * f.indexCountPerPrimitive * f.bytesPerIndex)
        let el = SCNGeometryElement(data: fData, primitiveType: .triangles, primitiveCount: f.count,
                                    bytesPerIndex: f.bytesPerIndex)
        let geo = SCNGeometry(sources: [vs], elements: [el])
        geo.materials = [material]
        return geo
    }

    /// Mesh as wires: additive, so overlapping and dense geometry reads brighter.
    private static func makeMeshMaterial() -> SCNMaterial {
        let m = SCNMaterial()
        m.lightingModel = .constant
        m.diffuse.contents = UIColor(white: 0.34, alpha: 1)
        m.fillMode = .lines
        m.isDoubleSided = true
        m.blendMode = .add
        m.writesToDepthBuffer = false
        return m
    }

    private static func makePointMaterial() -> SCNMaterial {
        let m = SCNMaterial()
        m.lightingModel = .constant
        m.diffuse.contents = Theme.UI.ink
        m.blendMode = .add
        m.writesToDepthBuffer = false
        return m
    }

    /// Inside-out `bg` sphere around the camera, drawn first and without depth: hides the camera
    /// feed regardless of how ARSCNView manages `scene.background`.
    private static func makeDome() -> SCNNode {
        let s = SCNSphere(radius: 20)
        s.segmentCount = 12
        let m = SCNMaterial()
        m.lightingModel = .constant
        m.diffuse.contents = Theme.UI.bg
        m.cullMode = .front
        m.writesToDepthBuffer = false
        m.readsFromDepthBuffer = false
        s.materials = [m]
        let n = SCNNode(geometry: s)
        n.name = "stage-dome"
        n.renderingOrder = -1000
        n.isHidden = true
        return n
    }
}

/// X/Y/Z at the origin: 1 px lines (SceneKit line primitives) of 0.3 m and tiny billboard labels.
enum MarkerAxes {
    static let length: Float = 0.3

    static func make() -> SCNNode {
        let root = SCNNode()
        root.name = "marker-axes"
        let l = CGFloat(length)
        let verts = [SCNVector3(0, 0, 0), SCNVector3(l, 0, 0),
                     SCNVector3(0, 0, 0), SCNVector3(0, l, 0),
                     SCNVector3(0, 0, 0), SCNVector3(0, 0, l)]
        let geo = SCNGeometry(sources: [SCNGeometrySource(vertices: verts)],
                              elements: [SCNGeometryElement(indices: [UInt16](0..<6), primitiveType: .line)])
        let m = SCNMaterial()
        m.lightingModel = .constant
        m.diffuse.contents = Theme.UI.ink
        m.readsFromDepthBuffer = false
        m.writesToDepthBuffer = false
        geo.materials = [m]
        let lines = SCNNode(geometry: geo)
        lines.renderingOrder = 100
        root.addChildNode(lines)
        for (name, dir) in [("X", SIMD3<Float>(1, 0, 0)), ("Y", SIMD3<Float>(0, 1, 0)), ("Z", SIMD3<Float>(0, 0, 1))] {
            let n = label(name)
            n.simdPosition = dir * (length + 0.025)
            root.addChildNode(n)
        }
        root.isHidden = true
        return root
    }

    /// A camera-facing quad with the letter rendered into a texture (crisper than SCNText).
    private static func label(_ text: String) -> SCNNode {
        let px: CGFloat = 96
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = false
        let image = UIGraphicsImageRenderer(size: CGSize(width: px, height: px), format: format).image { _ in
            let attrs: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: 54, weight: .light),
                .foregroundColor: Theme.UI.ink,
            ]
            let s = NSAttributedString(string: text, attributes: attrs)
            let size = s.size()
            s.draw(at: CGPoint(x: (px - size.width) / 2, y: (px - size.height) / 2))
        }
        let plane = SCNPlane(width: 0.035, height: 0.035)
        let m = SCNMaterial()
        m.lightingModel = .constant
        m.diffuse.contents = image
        m.isDoubleSided = true
        m.readsFromDepthBuffer = false
        m.writesToDepthBuffer = false
        plane.materials = [m]
        let n = SCNNode(geometry: plane)
        n.renderingOrder = 101
        n.constraints = [SCNBillboardConstraint()]
        n.name = "axis-\(text)"
        return n
    }
}
