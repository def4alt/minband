import ARKit
import Foundation
import Network

/// bbox center -> ray -> depth sample (median patch) or plane raycast -> marker frame.
enum Lift3D {
    static func lift(_ dets: [Detection], frame: ARFrame) -> [WorldPoint] { [] } // TODO(M4)
}
