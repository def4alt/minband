import ARKit
import Foundation
import Network

/// 3D nearest-neighbour association + constant-velocity Kalman per track. Birth after 3 hits,
/// death after 1 s. Velocity quality here decides how often the core sends deltas.
final class Tracker {
    func update(_ points: [WorldPoint], time: TimeInterval) {} // TODO(M4)
    func tracks(at time: TimeInterval) -> [Track] { [] }       // TODO(M4)
}
