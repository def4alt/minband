import ARKit
import Foundation
import Network

struct Detection { let classId: UInt8; let bbox: CGRect; let conf: Float }
struct Track { let id: UInt32; let classId: UInt8; let pos: SIMD3<Float>; let vel: SIMD3<Float>; let conf: UInt8 }
struct WorldPoint { let classId: UInt8; let pos: SIMD3<Float>; let conf: Float }
