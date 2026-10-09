import ARKit
import Foundation
import Network

/// World origin = marker. Converts ARKit world coords to marker frame (Y up, origin at marker).
final class Origin {
    static let shared = Origin()
    private(set) var isLocked = false
    private var markerToWorld = matrix_identity_float4x4
    func lock(markerTransform: simd_float4x4) { markerToWorld = markerTransform; isLocked = true }
    func toMarker(_ p: SIMD3<Float>) -> SIMD3<Float> {
        let w = markerToWorld.inverse * SIMD4<Float>(p, 1); return SIMD3(w.x, w.y, w.z)
    }
}
