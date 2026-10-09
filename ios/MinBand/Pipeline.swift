import ARKit
import Combine
import Foundation

/// Orchestrates ARKit -> detector -> tracker -> core Edge -> UDP. One instance per app.
///
/// Threads
/// - `arQueue` (serial): ARSession delegate callbacks, and everything that touches `edge`,
///   `transport`, `groundTruth` and `videoBaseline`. `start`/`stop` hop onto it synchronously.
/// - `detectQueue` (serial): Vision/CoreML, 3D lift and `tracker.update`, at most one frame in
///   flight (~12 Hz), so a slow model never blocks ARKit or makes it drop frames.
/// - `VideoBaseline`'s own queue (opt-in H.264 baseline): scaling and encoding, one frame in flight.
/// - main: @Published UI state only.
///
/// Rates: detection ~12 Hz, `tracker.tracks(at:)` -> ground truth -> `edge.tick` at 30 Hz,
/// Pose offered to the core at 2 Hz (the core sends one only when its budget-derived pose
/// interval is due, S19), HUD stats at 2 Hz, H.264 baseline frames at 30 Hz. Before the origin is
/// locked the edge is ticked with no tracks (it sends only Hello) and no Pose is offered.
final class Pipeline: NSObject, ObservableObject, ARSessionDelegate {
    @Published var originLocked = false
    @Published var originSource = "none"         // none | marker | manual
    @Published var trackCount = 0
    @Published var bytesPerSec = 0               // core estimate (payload)
    @Published var wireBytesPerSec = 0           // measured, payload + 28 B UDP/IP per datagram
    @Published var seq: UInt32 = 0
    @Published var thetaScale: Double = 1
    @Published var running = false
    @Published var detections: [OverlayBox] = []
    @Published var fps: Double = 0               // ARKit frames/s
    @Published var detectHz: Double = 0          // detector runs/s
    @Published var detectorStatus = "loading model"
    @Published var depthMode = "-"               // lidar | planes
    @Published var status = ""                   // tracking state / errors
    // UI only (no effect on perception or sync):
    @Published private(set) var wireframe = false        // stage mode, see setWireframe(_:)
    @Published var featurePoints = 0             // ARFrame.rawFeaturePoints count, 2 Hz
    @Published var meshAnchors = 0               // ARMeshAnchor count (scene reconstruction), 2 Hz
    @Published var liftMarks: [LiftMark] = []    // lifted 3D points of tracked boxes, in the view
    @Published var arUnsupported = false         // start() refused: no world tracking (simulator)
    @Published var link: LinkState = .off        // from datagrams received from the server, 2 Hz
    @Published var baselineNote = ""             // H.264 baseline: recording / finishing / kbps / error

    /// LiDAR scene reconstruction, used by the WIREFRAME stage mode.
    static let supportsMesh = ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)

    static let detectInterval: TimeInterval = 1.0 / 12
    /// No datagram from the server for this long (after at least one) = link lost. The server acks
    /// every delivered datagram (at most every 100 ms) and an acked edge sends at least a Hello
    /// refresh every 5 s, so silence beyond 7 s means the server or the network is gone.
    static let linkLostAfter: TimeInterval = 7
    static let trackInterval: TimeInterval = 1.0 / 30
    /// How often a Pose is offered to the core. The core decides which are sent (S19: its pose
    /// interval follows the byte budget) and returns empty Data for the others.
    static let poseInterval: TimeInterval = 0.5
    static let statsInterval: TimeInterval = 0.5
    static let ticksPerSecond: Double = 120
    /// Re-lock the origin on marker updates only for real corrections, not per-frame jitter.
    static let relockTranslation: Float = 0.02
    static let relockAngle: Float = 1.0 * .pi / 180
    /// Origin moves beyond this (or source changes) invalidate existing tracks.
    static let resetTracksTranslation: Float = 0.25

    let session = ARSession()
    private let tracker = Tracker()
    private let arQueue = DispatchQueue(label: "minband.ar", qos: .userInteractive)
    private let detectQueue = DispatchQueue(label: "minband.detect", qos: .userInitiated)
    private var detector: Detector?              // detectQueue only

    // arQueue only
    private var transport: UdpTransport?
    private var edge: EdgeBridge?
    private var groundTruth: GroundTruthLog?
    private var videoBaseline: VideoBaseline?    // opt-in H.264 baseline for this run
    private var isRunning = false
    private var sessionStart: TimeInterval = 0
    private var lastDetectionTime: TimeInterval = 0
    private var lastTrackTime: TimeInterval = 0
    private var lastPoseTime: TimeInterval = 0
    private var lastStatsTime: TimeInterval = 0
    private var detecting = false
    private var framesSinceStats = 0
    private var detectionsSinceStats = 0
    private var sentBytesAtStats = 0
    private var datagramsSinceStats = 0
    private var lastTrackCount = 0
    private var lastCamera: simd_float4x4?
    private var lastReceive: TimeInterval = 0    // systemUptime of the last datagram from the server
    /// main only: the configuration the session runs, re-run with changes by setWireframe(_:).
    private var runningConfig: ARWorldTrackingConfiguration?
    /// main only: bumped by every start, so a late H.264 result never overwrites a newer run's note.
    private var baselineRun = 0

    private let viewportLock = NSLock()
    private var _viewport = CGSize(width: 390, height: 844)
    /// Size of the AR view in points; the overlay boxes are mapped for it.
    var viewportSize: CGSize {
        get { viewportLock.withLock { _viewport } }
        set { viewportLock.withLock { _viewport = newValue } }
    }

    override init() {
        super.init()
        session.delegate = self
        session.delegateQueue = arQueue
        detectQueue.async { [weak self] in
            let d = Detector()
            self?.detector = d
            let s = d.isAvailable ? "model \(d.modelName ?? "?")" : "no detector: \(d.lastError ?? "no model")"
            DispatchQueue.main.async { self?.detectorStatus = s }
        }
    }

    // MARK: control (main thread)

    /// `videoBaseline`: also record the H.264 baseline (720p/480p/360p) for this run, see
    /// `VideoBaseline`. Off by default: three encoders cost battery and thermal headroom.
    func start(host: String, videoBaseline recordVideo: Bool = false) {
        guard ARWorldTrackingConfiguration.isSupported else {
            status = "ARKit world tracking is not supported on this device"
            arUnsupported = true
            return
        }
        let config = ARWorldTrackingConfiguration()
        let lidar = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
        if lidar { config.frameSemantics.insert(.sceneDepth) }
        config.planeDetection = [.horizontal]
        config.detectionImages = ARReferenceImage.referenceImages(inGroupNamed: "Markers", bundle: nil) ?? []
        config.maximumNumberOfTrackedImages = 1   // keep refining the marker pose while visible
        if config.detectionImages.isEmpty { status = "no Markers AR resource group in the bundle" }
        if wireframe, Pipeline.supportsMesh { config.sceneReconstruction = .mesh }
        runningConfig = config

        let (h, port) = UdpTransport.parse(host)
        let baselineError: String? = arQueue.sync {
            Origin.shared.reset()
            tracker.reset()
            sessionStart = 0
            lastDetectionTime = 0; lastTrackTime = 0; lastPoseTime = 0; lastStatsTime = 0
            framesSinceStats = 0; detectionsSinceStats = 0; datagramsSinceStats = 0; sentBytesAtStats = 0
            lastReceive = 0
            let t = UdpTransport(host: h, port: port) { [weak self] bytes in
                self?.arQueue.async {
                    self?.lastReceive = ProcessInfo.processInfo.systemUptime
                    self?.edge?.onDatagram(bytes)
                }
            }
            transport = t
            edge = EdgeBridge(deviceId: DeviceIdentity.id, sessionNonce: UInt32.random(in: 1...UInt32.max))
            let gt = GroundTruthLog()
            groundTruth = gt
            videoBaseline = nil
            var failure: String?
            if recordVideo {
                do { videoBaseline = try VideoBaseline(log: gt.url) } catch { failure = error.localizedDescription }
            }
            isRunning = true
            return failure
        }
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
        baselineRun += 1
        if !recordVideo {
            baselineNote = ""
        } else if let baselineError {
            baselineNote = "h.264 baseline unavailable: \(baselineError)"
        } else {
            baselineNote = "recording h.264 baseline"
        }
        running = true
        link = .waiting
        originLocked = false; originSource = "none"
        depthMode = lidar ? "lidar" : "planes"
    }

    func stop() {
        session.pause()
        let baseline: VideoBaseline? = arQueue.sync {
            isRunning = false
            transport?.close(); groundTruth?.close()
            let b = videoBaseline
            transport = nil; edge = nil; groundTruth = nil; videoBaseline = nil
            return b
        }
        if let baseline { finishBaseline(baseline) }
        running = false
        link = .off
        runningConfig = nil
        detections = []
        liftMarks = []
        trackCount = 0
    }

    /// WIREFRAME stage mode on/off. The view hides the camera and draws the reconstruction mesh
    /// and feature points; here only LiDAR scene reconstruction is switched, by re-running the
    /// same configuration without reset options, so tracking, anchors, the locked origin and the
    /// edge session all carry on. Without LiDAR (or while stopped) this only flips the flag.
    func setWireframe(_ on: Bool) {
        wireframe = on
        guard running, Pipeline.supportsMesh, let current = runningConfig,
              let config = current.copy() as? ARWorldTrackingConfiguration else { return }
        let mode: ARConfiguration.SceneReconstruction = on ? .mesh : []
        guard config.sceneReconstruction != mode else { return }
        config.sceneReconstruction = mode
        runningConfig = config
        session.run(config)
    }

    /// "origin: set here" fallback: camera position dropped to the floor plane, Y up.
    func setOriginHere() {
        arQueue.async { [weak self] in
            guard let self, let cam = self.lastCamera else { return }
            Origin.shared.lockManual(cameraTransform: cam)
            self.tracker.reset()
            self.publishOrigin()
        }
    }

    /// Finishes and measures the H.264 files in the background (about a second), then shows the
    /// measured kbps (or the error) as a notice until the next start.
    private func finishBaseline(_ baseline: VideoBaseline) {
        let run = baselineRun
        baselineNote = "h.264 baseline finishing"
        baseline.finish { [weak self] outcome in
            let note = outcome.summary
            DispatchQueue.main.async {
                guard let self, self.baselineRun == run else { return }
                self.baselineNote = note
            }
        }
    }

    /// Writes buffered ground-truth rows so the file can be shared.
    func flushLog() { arQueue.sync { groundTruth?.flush() } }

    // MARK: ARSessionDelegate (arQueue)

    func session(_ session: ARSession, didAdd anchors: [ARAnchor]) { handle(anchors, added: true) }
    func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) { handle(anchors, added: false) }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard isRunning, let edge else { return }
        let ts = frame.timestamp
        if sessionStart == 0 { sessionStart = ts; lastStatsTime = ts }
        let tick = UInt32(max(0, (ts - sessionStart) * Pipeline.ticksPerSecond))
        lastCamera = frame.camera.transform
        framesSinceStats += 1
        let locked = Origin.shared.isLocked

        // Opt-in H.264 baseline: hands the captured image to its own queue at ~30 Hz and returns.
        videoBaseline?.offer(frame.capturedImage, time: ts)

        // Detection at ~12 Hz on a background queue, never more than one frame in flight.
        if !detecting, ts - lastDetectionTime >= Pipeline.detectInterval - 0.004 {
            detecting = true
            lastDetectionTime = ts
            runDetection(frame, locked: locked)
        }

        // Tracks -> ground truth -> core at 30 Hz.
        if ts - lastTrackTime >= Pipeline.trackInterval - 0.004 {
            lastTrackTime = ts
            let tracks = locked ? tracker.tracks(at: ts) : []
            if locked { groundTruth?.append(tick: tick, tracks: tracks) }
            for d in edge.tick(tracks: tracks, now: tick) { send(d) }
            lastTrackCount = tracks.count
        }

        // Pose offered at 2 Hz, only once the origin means something; the core returns empty Data
        // unless a Pose is due (its interval follows the budget, S19), and `send` drops that. The
        // bridge converts the ARKit world camera transform into the marker frame
        // (Origin.toMarker / rotationToMarker).
        if locked, ts - lastPoseTime >= Pipeline.poseInterval {
            lastPoseTime = ts
            send(edge.pose(frame.camera.transform, tick: tick))
        }

        if ts - lastStatsTime >= Pipeline.statsInterval { publishStats(now: ts, edge: edge, frame: frame) }
    }

    func session(_ session: ARSession, cameraDidChangeTrackingState camera: ARCamera) {
        let s: String
        switch camera.trackingState {
        case .normal: s = ""
        case .notAvailable: s = "tracking not available"
        case .limited(let r):
            switch r {
            case .initializing: s = "initializing, move the phone slowly"
            case .excessiveMotion: s = "slow down"
            case .insufficientFeatures: s = "not enough texture"
            case .relocalizing: s = "relocalizing"
            @unknown default: s = "tracking limited"
            }
        }
        DispatchQueue.main.async { self.status = s }
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        DispatchQueue.main.async { self.status = "AR error: \(error.localizedDescription)" }
    }

    func sessionWasInterrupted(_ session: ARSession) {
        DispatchQueue.main.async { self.status = "AR session interrupted" }
    }

    // MARK: internals

    /// Empty Data means "nothing due" (a Pose before the first ack or between the core's pose
    /// intervals) and is never put on the wire as an empty datagram.
    private func send(_ d: Data) {
        guard !d.isEmpty, let transport else { return }
        transport.send(d)
        datagramsSinceStats += 1
    }

    private func handle(_ anchors: [ARAnchor], added: Bool) {
        for a in anchors {
            if let img = a as? ARImageAnchor {
                let origin = Origin.shared
                var shouldLock = added || origin.source != .marker
                if !shouldLock, img.isTracked, let d = origin.difference(markerTransform: img.transform) {
                    shouldLock = d.translation > Pipeline.relockTranslation || d.angle > Pipeline.relockAngle
                }
                guard shouldLock, added || img.isTracked else { continue }
                let change = origin.lock(markerTransform: img.transform)
                if !change.wasLocked || change.previousSource != .marker || change.translation > Pipeline.resetTracksTranslation {
                    tracker.reset()
                }
                publishOrigin()
            } else if let plane = a as? ARPlaneAnchor, plane.alignment == .horizontal {
                var isFloor = false
                if ARPlaneAnchor.isClassificationSupported {
                    switch plane.classification {
                    case .floor: isFloor = true
                    case .ceiling, .table, .seat: continue
                    default: break
                    }
                }
                let c = plane.transform * SIMD4<Float>(plane.center, 1)
                // Upward-facing planes well below the camera only (skip tables when unclassified).
                if !isFloor, let cam = lastCamera, c.y > cam.columns.3.y - 0.8 { continue }
                Origin.shared.observeHorizontalPlane(y: c.y, isFloor: isFloor)
            }
        }
    }

    private func runDetection(_ frame: ARFrame, locked: Bool) {
        let viewport = viewportSize
        detectQueue.async { [weak self] in
            guard let self else { return }
            let dets = self.detector?.detect(frame) ?? []
            var trackIds = [UInt32?](repeating: nil, count: dets.count)
            var marks: [LiftMark] = []
            if locked {
                let points = Lift3D.lift(dets, frame: frame, session: self.session)
                let ids = self.tracker.update(points, time: frame.timestamp)
                for (p, id) in zip(points, ids) where dets.indices.contains(p.detectionIndex) {
                    trackIds[p.detectionIndex] = id
                }
                marks = Pipeline.liftMarks(points, ids: ids, frame: frame, viewport: viewport)
            }
            let boxes = Pipeline.overlay(dets, trackIds: trackIds, frame: frame, viewport: viewport)
            self.arQueue.async { self.detecting = false; self.detectionsSinceStats += 1 }
            DispatchQueue.main.async { if self.running { self.detections = boxes; self.liftMarks = marks } }
        }
    }

    /// Captured-image boxes -> normalized view rects for a portrait, aspect-filled AR view.
    static func overlay(_ dets: [Detection], trackIds: [UInt32?], frame: ARFrame, viewport: CGSize) -> [OverlayBox] {
        guard viewport.width > 0, viewport.height > 0 else { return [] }
        let t = frame.displayTransform(for: .portrait, viewportSize: viewport)
        return dets.enumerated().map { i, d in
            OverlayBox(id: i, rect: d.bbox.applying(t).standardized, classId: d.classId, conf: d.conf,
                       trackId: i < trackIds.count ? trackIds[i] : nil)
        }
    }

    /// Lifted points (marker frame) of boxes that fed a confirmed track -> normalized view points,
    /// for the overlay's `+` marks. Display only; points behind the camera are skipped.
    static func liftMarks(_ points: [WorldPoint], ids: [UInt32?], frame: ARFrame, viewport: CGSize) -> [LiftMark] {
        guard viewport.width > 0, viewport.height > 0 else { return [] }
        let markerToWorld = Origin.shared.markerTransform
        let worldToCamera = frame.camera.transform.inverse
        var out: [LiftMark] = []
        for (p, id) in zip(points, ids) {
            guard let id else { continue }
            let w = markerToWorld * SIMD4<Float>(p.pos, 1)
            guard (worldToCamera * w).z < 0 else { continue }
            let s = frame.camera.projectPoint(SIMD3(w.x, w.y, w.z), orientation: .portrait, viewportSize: viewport)
            out.append(LiftMark(id: p.detectionIndex, trackId: id,
                                point: CGPoint(x: s.x / viewport.width, y: s.y / viewport.height)))
        }
        return out
    }

    private func publishOrigin() {
        let o = Origin.shared
        let locked = o.isLocked
        let src: String = { switch o.source { case .none: return "none"; case .marker: return "marker"; case .manual: return "manual" } }()
        DispatchQueue.main.async { self.originLocked = locked; self.originSource = src }
    }

    private func publishStats(now: TimeInterval, edge: EdgeBridge, frame: ARFrame) {
        let dt = max(1e-3, now - lastStatsTime)
        let s = edge.stats()
        let sent = transport?.sentBytes ?? 0
        let wire = Int((Double(sent - sentBytesAtStats + 28 * datagramsSinceStats) / dt).rounded())
        let fps = Double(framesSinceStats) / dt
        let dhz = Double(detectionsSinceStats) / dt
        let count = lastTrackCount
        let points = frame.rawFeaturePoints?.points.count ?? 0
        let meshes = frame.anchors.reduce(0) { $0 + ($1 is ARMeshAnchor ? 1 : 0) }
        let link: LinkState = lastReceive == 0 ? .waiting
            : ProcessInfo.processInfo.systemUptime - lastReceive > Pipeline.linkLostAfter ? .lost : .up
        lastStatsTime = now; framesSinceStats = 0; detectionsSinceStats = 0
        sentBytesAtStats = sent; datagramsSinceStats = 0
        DispatchQueue.main.async {
            self.trackCount = count; self.seq = s.seq; self.thetaScale = s.thetaScale
            self.bytesPerSec = s.bytesPerSecEstimate; self.wireBytesPerSec = wire
            self.fps = fps; self.detectHz = dhz
            self.featurePoints = points; self.meshAnchors = meshes
            if self.running { self.link = link }
        }
    }
}

/// A box that fed a confirmed track, with its lifted 3D point projected back into the view
/// (normalized view coordinates, origin top-left, like `OverlayBox.rect`). `id` is the box index.
struct LiftMark: Identifiable {
    let id: Int
    let trackId: UInt32
    let point: CGPoint
}

/// What the operator needs to know about the UDP link. `waiting`: started, nothing heard from the
/// server yet (the edge is sending Hello). `up`: the server answered within `linkLostAfter`.
/// `lost`: it answered before but has been silent since.
enum LinkState { case off, waiting, up, lost }
