import ARKit
import Foundation
import simd

/// bbox center -> ray -> depth sample (median patch) or plane raycast -> marker frame.
///
/// Positions follow the viewer's convention (viewer/src/scene.ts draws a person capsule *above*
/// `pos` and boxes above `pos`): a person is reported at their feet.
/// - Depth (LiDAR `sceneDepth`): median of a 5x5 patch at the bbox centre, `.low` confidence
///   ignored, unprojected with `ARCamera.intrinsics`. For a person the centre is the torso (the
///   most reliable pixel), and the point is dropped to the floor height (lowest detected plane,
///   else the marker plane y = 0) to get the feet.
/// - No depth (or no valid sample): `ARSession.raycast` from the bbox centre, and for a person from
///   the bottom-centre (where the feet meet the floor; "bottom" is gravity-down in the image, so it
///   works for any device roll), against estimated horizontal planes.
/// - Neither: the detection is dropped.
enum Lift3D {
    static let patchRadius = 2          // 5x5
    static let minValidSamples = 5      // of 25
    static let depthRange: ClosedRange<Float> = 0.15...8.0
    /// A person whose bottom edge is this close to the image border has no visible feet.
    static let borderMargin: CGFloat = 0.015

    static func lift(_ dets: [Detection], frame: ARFrame, session: ARSession? = nil,
                     origin: Origin = .shared) -> [WorldPoint] {
        guard !dets.isEmpty else { return [] }
        let cam = frame.camera
        let res = SIMD2<Float>(Float(cam.imageResolution.width), Float(cam.imageResolution.height))
        let depth = DepthSampler(frame.sceneDepth ?? frame.smoothedSceneDepth)
        let down = downDirectionInImage(cameraTransform: cam.transform)
        let floorInMarker = origin.floorHeightInMarker
        let camPos = SIMD3<Float>(cam.transform.columns.3.x, cam.transform.columns.3.y, cam.transform.columns.3.z)

        var out: [WorldPoint] = []
        for (i, d) in dets.enumerated() {
            let isPerson = d.classId == TrackedClass.person
            let centre = CGPoint(x: d.bbox.midX, y: d.bbox.midY)
            var world: SIMD3<Float>?
            var fromDepth = false

            if let z = depth?.median(at: centre, radius: patchRadius, minValid: minValidSamples),
               depthRange.contains(z) {
                let pc = unproject(normalized: centre, depth: z, intrinsics: cam.intrinsics, imageResolution: res)
                let w = cam.transform * SIMD4<Float>(pc, 1)
                world = SIMD3(w.x, w.y, w.z)
                fromDepth = true
            } else if let session {
                var p = centre
                if isPerson {
                    p = bottomPoint(of: d.bbox, down: down, imageResolution: res)
                    if p.x < borderMargin || p.y < borderMargin || p.x > 1 - borderMargin || p.y > 1 - borderMargin { continue }
                }
                world = raycast(session: session, frame: frame, point: p, infiniteFallback: isPerson)
                if let w = world, simd_distance(w, camPos) > depthRange.upperBound { world = nil }
            }
            guard let w = world else { continue }
            var m = origin.toMarker(w)
            if isPerson {
                // Depth hit the torso: drop to the floor. A raycast already hit the floor at the
                // feet; snap it to the known floor height too so both paths agree.
                if let f = floorInMarker { m.y = f } else if fromDepth { m.y = 0 }
            }
            out.append(WorldPoint(classId: d.classId, pos: m, conf: d.conf, detectionIndex: i))
        }
        return out
    }

    // MARK: pure helpers (unit-tested)

    /// Normalized captured-image point + z-depth -> point in ARKit camera space (x right, y up,
    /// z backward, in the sensor's landscape frame). Intrinsics are for `imageResolution` pixels.
    static func unproject(normalized p: CGPoint, depth z: Float, intrinsics k: simd_float3x3,
                          imageResolution res: SIMD2<Float>) -> SIMD3<Float> {
        let u = Float(p.x) * res.x, v = Float(p.y) * res.y
        let fx = k.columns.0.x, fy = k.columns.1.y, cx = k.columns.2.x, cy = k.columns.2.y
        let x = (u - cx) / fx * z
        let y = (v - cy) / fy * z
        // Pinhole (x right, y down, z forward) -> ARKit camera (x right, y up, z backward).
        return SIMD3(x, -y, -z)
    }

    /// Unit direction (in pixels: x right, y down) of world gravity in the captured image.
    static func downDirectionInImage(cameraTransform t: simd_float4x4) -> SIMD2<Float> {
        let r = simd_float3x3(SIMD3(t.columns.0.x, t.columns.0.y, t.columns.0.z),
                              SIMD3(t.columns.1.x, t.columns.1.y, t.columns.1.z),
                              SIMD3(t.columns.2.x, t.columns.2.y, t.columns.2.z))
        let dc = r.transpose * SIMD3<Float>(0, -1, 0)      // world down in camera axes
        let d = SIMD2<Float>(dc.x, -dc.y)                   // camera y up -> image y down
        let l = simd_length(d)
        // Looking straight up/down: fall back to portrait "down" (+x of the sensor image).
        return l < 1e-3 ? SIMD2(1, 0) : d / l
    }

    /// Point just inside `box` from its centre along `down` (the feet of an upright person).
    static func bottomPoint(of box: CGRect, down: SIMD2<Float>, imageResolution res: SIMD2<Float>) -> CGPoint {
        let c = SIMD2<Float>(Float(box.midX) * res.x, Float(box.midY) * res.y)
        let hw = Float(box.width) * res.x / 2, hh = Float(box.height) * res.y / 2
        let tx = abs(down.x) > 1e-6 ? hw / abs(down.x) : .infinity
        let ty = abs(down.y) > 1e-6 ? hh / abs(down.y) : .infinity
        let p = c + down * (min(tx, ty) * 0.97)
        return CGPoint(x: CGFloat(p.x / res.x), y: CGFloat(p.y / res.y))
    }

    static func median(_ v: inout [Float]) -> Float? {
        guard !v.isEmpty else { return nil }
        v.sort()
        let n = v.count
        return n % 2 == 1 ? v[n / 2] : (v[n / 2 - 1] + v[n / 2]) / 2
    }

    // MARK: ARKit plumbing

    private static func raycast(session: ARSession, frame: ARFrame, point: CGPoint, infiniteFallback: Bool) -> SIMD3<Float>? {
        var q = frame.raycastQuery(from: point, allowing: .estimatedPlane, alignment: .horizontal)
        var hit = session.raycast(q).first
        if hit == nil, infiniteFallback {
            q = frame.raycastQuery(from: point, allowing: .existingPlaneInfinite, alignment: .horizontal)
            hit = session.raycast(q).first
        }
        guard let t = hit?.worldTransform else { return nil }
        return SIMD3(t.columns.3.x, t.columns.3.y, t.columns.3.z)
    }
}

/// Reads `ARDepthData` (Float32 depth + UInt8 ARConfidenceLevel map, same orientation as the
/// captured image) and returns robust patch medians.
struct DepthSampler {
    private let depth: CVPixelBuffer
    private let confidence: CVPixelBuffer?

    init?(_ data: ARDepthData?) {
        guard let data, CVPixelBufferGetPixelFormatType(data.depthMap) == kCVPixelFormatType_DepthFloat32 else { return nil }
        depth = data.depthMap
        confidence = data.confidenceMap.flatMap {
            CVPixelBufferGetPixelFormatType($0) == kCVPixelFormatType_OneComponent8 ? $0 : nil
        }
    }

    func median(at p: CGPoint, radius r: Int, minValid: Int) -> Float? {
        CVPixelBufferLockBaseAddress(depth, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depth, .readOnly) }
        if let confidence { CVPixelBufferLockBaseAddress(confidence, .readOnly) }
        defer { if let confidence { CVPixelBufferUnlockBaseAddress(confidence, .readOnly) } }

        let w = CVPixelBufferGetWidth(depth), h = CVPixelBufferGetHeight(depth)
        guard let dBase = CVPixelBufferGetBaseAddress(depth) else { return nil }
        let dRow = CVPixelBufferGetBytesPerRow(depth)
        let cBase = confidence.flatMap { CVPixelBufferGetBaseAddress($0) }
        let cRow = confidence.map { CVPixelBufferGetBytesPerRow($0) } ?? 0
        let cx = Int(p.x * CGFloat(w)), cy = Int(p.y * CGFloat(h))

        let x0 = max(0, cx - r), x1 = min(w - 1, cx + r), y0 = max(0, cy - r), y1 = min(h - 1, cy + r)
        guard x0 <= x1, y0 <= y1 else { return nil }

        var samples: [Float] = []
        samples.reserveCapacity((2 * r + 1) * (2 * r + 1))
        for y in y0...y1 {
            let dLine = (dBase + y * dRow).assumingMemoryBound(to: Float32.self)
            let cLine = cBase.map { ($0 + y * cRow).assumingMemoryBound(to: UInt8.self) }
            for x in x0...x1 {
                if let cLine, cLine[x] <= UInt8(ARConfidenceLevel.low.rawValue) { continue }
                let z = dLine[x]
                if z.isFinite && z > 0 { samples.append(z) }
            }
        }
        guard samples.count >= minValid else { return nil }
        return Lift3D.median(&samples)
    }
}
