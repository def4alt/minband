import Foundation
import simd

/// World origin = printed marker. Converts ARKit world coordinates into the marker frame that
/// every phone (and the wire, PROTOCOL.md "metres, marker frame, Y up") shares.
///
/// Conventions
/// - ARKit world: right-handed, metres, +Y up along gravity (`worldAlignment = .gravity`, the
///   default), origin and yaw arbitrary per session.
/// - `ARImageAnchor.transform` (marker -> world): origin at the image centre, +X along the image
///   width (to the right when the image is viewed upright), +Y the image normal pointing out of the
///   printed side, +Z = X x Y along the image height pointing toward the image's bottom edge.
///   So for a marker lying flat on the floor the anchor's +Y is already up.
/// - Marker frame (what we output): origin at the marker centre, +Y = world up (gravity),
///   +X = the image width axis projected onto the horizontal plane, +Z = X x Y (horizontal,
///   toward the marker's bottom edge). For a flat marker this is the anchor frame minus ARKit's
///   tilt estimation error, which we drop on purpose: a 1 degree tilt error would put an object
///   5 m away 9 cm too high or low, and gravity from the IMU is far better than the image normal.
///   A marker taped to a wall still yields a Y-up frame (X along its width, Z out of the wall).
/// - Manual fallback (`lockManual`): origin = camera position dropped onto the floor plane,
///   +X = the camera's right (portrait) direction levelled, +Y up, +Z = X x Y (toward the user).
///
/// Thread safety: read from the AR delegate queue, the detection queue and the bridge; all state
/// is behind a lock.
final class Origin {
    enum Source { case none, marker, manual }

    struct Change {
        let wasLocked: Bool
        let previousSource: Source
        let translation: Float   // metres the origin moved vs the previous lock
        let angle: Float         // radians the yaw changed vs the previous lock
    }

    static let shared = Origin()

    /// Assumed camera height above the floor when no horizontal plane has been seen yet.
    static let defaultCameraHeight: Float = 1.4

    private let lock = NSLock()
    private var _source: Source = .none
    private var markerToWorld = matrix_identity_float4x4
    private var worldToMarker = matrix_identity_float4x4
    private var classifiedFloorY: Float?
    private var lowestPlaneY: Float?

    init() {}

    var isLocked: Bool { lock.withLock { _source != .none } }
    var source: Source { lock.withLock { _source } }
    /// Floor height in ARKit world Y: the lowest plane ARKit classified as floor, else the lowest
    /// upward-facing horizontal plane seen this session.
    var floorY: Float? { lock.withLock { classifiedFloorY ?? lowestPlaneY } }
    /// Floor height in the marker frame (0 when the marker lies on the floor).
    var floorHeightInMarker: Float? {
        lock.withLock { (classifiedFloorY ?? lowestPlaneY).map { $0 - markerToWorld.columns.3.y } }
    }
    /// Marker -> ARKit world (columns: X, Y, Z axes and origin).
    var markerTransform: simd_float4x4 { lock.withLock { markerToWorld } }

    func reset() {
        lock.withLock { _source = .none; markerToWorld = matrix_identity_float4x4; worldToMarker = matrix_identity_float4x4
            classifiedFloorY = nil; lowestPlaneY = nil }
    }

    /// Lock to a detected marker. `markerTransform` is `ARImageAnchor.transform`.
    @discardableResult
    func lock(markerTransform: simd_float4x4) -> Change {
        set(Origin.levelledMarkerFrame(fromImageAnchor: markerTransform), source: .marker)
    }

    /// Fallback "set origin here": the current camera position projected onto the detected floor
    /// plane (or `defaultCameraHeight` below the camera if no plane is known), Y up.
    @discardableResult
    func lockManual(cameraTransform: simd_float4x4) -> Change {
        let floor = floorY
        return set(Origin.manualFrame(cameraTransform: cameraTransform, floorY: floor), source: .manual)
    }

    /// How far a new marker estimate is from the current lock (nil if not marker-locked). Used to
    /// re-lock only on meaningful corrections instead of jittering the origin every frame.
    func difference(markerTransform: simd_float4x4) -> (translation: Float, angle: Float)? {
        let cur: simd_float4x4? = lock.withLock { _source == .marker ? markerToWorld : nil }
        guard let cur else { return nil }
        return Origin.difference(cur, Origin.levelledMarkerFrame(fromImageAnchor: markerTransform))
    }

    /// Feed upward-facing horizontal plane heights (ARKit world Y; skip ceilings, tables, seats).
    func observeHorizontalPlane(y: Float, isFloor: Bool) {
        lock.withLock {
            if isFloor { classifiedFloorY = min(classifiedFloorY ?? y, y) }
            lowestPlaneY = min(lowestPlaneY ?? y, y)
        }
    }

    /// ARKit world point -> marker frame.
    func toMarker(_ p: SIMD3<Float>) -> SIMD3<Float> {
        let m = lock.withLock { worldToMarker }
        let w = m * SIMD4<Float>(p, 1)
        return SIMD3(w.x, w.y, w.z)
    }

    /// ARKit world direction (e.g. a velocity) -> marker frame.
    func toMarkerDirection(_ v: SIMD3<Float>) -> SIMD3<Float> {
        let m = lock.withLock { worldToMarker }
        let w = m * SIMD4<Float>(v, 0)
        return SIMD3(w.x, w.y, w.z)
    }

    /// Rotation that takes ARKit world vectors into the marker frame (world -> marker). For a
    /// camera orientation `q` (camera -> world) the marker-frame orientation is
    /// `rotationToMarker() * q`.
    func rotationToMarker() -> simd_quatf {
        let m = lock.withLock { worldToMarker }
        return simd_quatf(simd_float3x3(SIMD3(m.columns.0.x, m.columns.0.y, m.columns.0.z),
                                        SIMD3(m.columns.1.x, m.columns.1.y, m.columns.1.z),
                                        SIMD3(m.columns.2.x, m.columns.2.y, m.columns.2.z))).normalized
    }

    /// Full ARKit world transform (e.g. `ARCamera.transform`) -> marker frame.
    func toMarker(transform t: simd_float4x4) -> simd_float4x4 { lock.withLock { worldToMarker } * t }

    // MARK: pure helpers (unit-tested)

    static let worldUp = SIMD3<Float>(0, 1, 0)

    static func levelledMarkerFrame(fromImageAnchor t: simd_float4x4) -> simd_float4x4 {
        let ax = xyz(t.columns.0), ay = xyz(t.columns.1)
        var x = horizontal(ax)
        if simd_length(x) < 0.2 {
            // Width axis nearly vertical (marker on a wall turned 90 degrees): keep Y up, put Z
            // along the horizontal image normal, X = Y x Z.
            x = simd_cross(worldUp, horizontal(ay))
        }
        if simd_length(x) < 1e-4 { x = SIMD3(1, 0, 0) }
        return frame(origin: xyz(t.columns.3), x: simd_normalize(x))
    }

    static func manualFrame(cameraTransform c: simd_float4x4, floorY: Float?) -> simd_float4x4 {
        let cam = xyz(c.columns.3)
        // ARKit camera axes are in the landscape-right sensor frame: +Y_cam is the device's right
        // edge in portrait, +X_cam its bottom edge, -Z_cam the optical axis. +Y_cam stays
        // horizontal for any pitch in portrait (including looking straight down).
        var x = horizontal(xyz(c.columns.1))
        if simd_length(x) < 0.2 { x = horizontal(xyz(c.columns.0)) }
        if simd_length(x) < 1e-4 { x = SIMD3(1, 0, 0) }
        let y = floorY ?? (cam.y - defaultCameraHeight)
        return frame(origin: SIMD3(cam.x, y, cam.z), x: simd_normalize(x))
    }

    /// Translation and yaw difference between two levelled frames.
    static func difference(_ a: simd_float4x4, _ b: simd_float4x4) -> (translation: Float, angle: Float) {
        let dt = simd_distance(xyz(a.columns.3), xyz(b.columns.3))
        let c = max(-1, min(1, simd_dot(xyz(a.columns.0), xyz(b.columns.0))))
        return (dt, acos(c))
    }

    private static func frame(origin o: SIMD3<Float>, x: SIMD3<Float>) -> simd_float4x4 {
        let y = worldUp
        let z = simd_cross(x, y)
        return simd_float4x4(SIMD4(x, 0), SIMD4(y, 0), SIMD4(z, 0), SIMD4(o, 1))
    }

    private static func horizontal(_ v: SIMD3<Float>) -> SIMD3<Float> { v - simd_dot(v, worldUp) * worldUp }
    private static func xyz(_ v: SIMD4<Float>) -> SIMD3<Float> { SIMD3(v.x, v.y, v.z) }

    private func set(_ m: simd_float4x4, source: Source) -> Change {
        lock.withLock {
            let was = _source
            let d = was == .none ? (translation: Float.infinity, angle: Float.pi) : Origin.difference(markerToWorld, m)
            markerToWorld = m
            worldToMarker = Origin.inverseRigid(m)
            _source = source
            return Change(wasLocked: was != .none, previousSource: was, translation: d.translation, angle: d.angle)
        }
    }

    /// Exact inverse of a rotation + translation.
    static func inverseRigid(_ m: simd_float4x4) -> simd_float4x4 {
        let r = simd_float3x3(xyz(m.columns.0), xyz(m.columns.1), xyz(m.columns.2))
        let rt = r.transpose
        let t = -(rt * xyz(m.columns.3))
        return simd_float4x4(SIMD4(rt.columns.0, 0), SIMD4(rt.columns.1, 0), SIMD4(rt.columns.2, 0), SIMD4(t, 1))
    }
}
