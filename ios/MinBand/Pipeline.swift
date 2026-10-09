import ARKit
import Combine
import Foundation

/// Orchestrates ARKit -> detector -> tracker -> core Edge -> UDP. One instance per session.
/// M4 fills in the TODOs; the structure is fixed here so the team can work in parallel.
final class Pipeline: NSObject, ObservableObject, ARSessionDelegate {
    @Published var originLocked = false
    @Published var trackCount = 0
    @Published var bytesPerSec = 0
    @Published var seq: UInt32 = 0
    @Published var thetaScale: Double = 1
    @Published var running = false

    let session = ARSession()
    private let detector = Detector()
    private let tracker = Tracker()
    private var transport: UdpTransport?
    private var edge: EdgeBridge?
    private var groundTruth: GroundTruthLog?
    private var sessionStart: TimeInterval = 0
    private var lastDetectionTime: TimeInterval = 0

    func start(host: String) {
        let config = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { config.frameSemantics.insert(.sceneDepth) }
        config.planeDetection = [.horizontal]
        config.detectionImages = ARReferenceImage.referenceImages(inGroupNamed: "Markers", bundle: nil) ?? []
        session.delegate = self
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
        sessionStart = 0
        transport = UdpTransport(host: host, port: 7777) { [weak self] bytes in self?.edge?.onDatagram(bytes) }
        edge = EdgeBridge(deviceId: DeviceIdentity.id, sessionNonce: UInt32.random(in: 1...UInt32.max))
        groundTruth = GroundTruthLog()
        running = true
    }

    func stop() {
        session.pause(); transport?.close(); groundTruth?.close()
        transport = nil; edge = nil; running = false
    }

    // MARK: ARSessionDelegate

    func session(_ session: ARSession, didAdd anchors: [ARAnchor]) {
        for a in anchors where a is ARImageAnchor { Origin.shared.lock(markerTransform: a.transform); originLocked = true }
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard let edge, running else { return }
        if sessionStart == 0 { sessionStart = frame.timestamp }
        let tick = UInt32((frame.timestamp - sessionStart) * 120)

        // Detection at ~12 Hz; tracker runs every frame.
        if frame.timestamp - lastDetectionTime > 1.0 / 12.0 {
            lastDetectionTime = frame.timestamp
            let dets = detector.detect(frame)                       // TODO(M4): Vision + CoreML
            let points = Lift3D.lift(dets, frame: frame)            // TODO(M4): depth / plane raycast -> marker frame
            tracker.update(points, time: frame.timestamp)
        }
        let tracks = tracker.tracks(at: frame.timestamp)            // id, class, pos, vel, conf
        groundTruth?.append(tick: tick, tracks: tracks)

        let datagrams = edge.tick(tracks: tracks, now: tick)
        for d in datagrams { transport?.send(d) }

        if tick % 60 == 0 {
            let s = edge.stats()
            DispatchQueue.main.async {
                self.trackCount = tracks.count; self.seq = s.seq; self.thetaScale = s.thetaScale; self.bytesPerSec = s.bytesPerSecEstimate
            }
        }
        if tick % 60 == 0, Origin.shared.isLocked { transport?.send(edge.pose(frame.camera.transform, tick: tick)) }
    }
}
