import ARKit
import Foundation
import Network

// Skeletons for the M4 components. Each is small enough to own independently.

struct Detection { let classId: UInt8; let bbox: CGRect; let conf: Float }
struct Track { let id: UInt32; let classId: UInt8; let pos: SIMD3<Float>; let vel: SIMD3<Float>; let conf: UInt8 }
struct WorldPoint { let classId: UInt8; let pos: SIMD3<Float>; let conf: Float }

/// Vision + CoreML YOLO on the ARFrame's captured image. 320 px input, COCO subset.
final class Detector {
    func detect(_ frame: ARFrame) -> [Detection] { [] } // TODO(M4)
}

/// bbox center -> ray -> depth sample (median patch) or plane raycast -> marker frame.
enum Lift3D {
    static func lift(_ dets: [Detection], frame: ARFrame) -> [WorldPoint] { [] } // TODO(M4)
}

/// 3D nearest-neighbour association + constant-velocity Kalman per track. Birth after 3 hits,
/// death after 1 s. Velocity quality here decides how often the core sends deltas.
final class Tracker {
    func update(_ points: [WorldPoint], time: TimeInterval) {} // TODO(M4)
    func tracks(at time: TimeInterval) -> [Track] { [] }       // TODO(M4)
}

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

/// Thin wrapper over the uniffi-generated bindings (ios/MinBand/Generated). Until M1 wires
/// uniffi, this compiles with the empty stubs so the app can run with the AR preview.
final class EdgeBridge {
    struct Stats { var seq: UInt32 = 0; var thetaScale: Double = 1; var bytesPerSecEstimate: Int = 0 }
    init(deviceId: UInt32, sessionNonce: UInt32) {}
    func tick(tracks: [Track], now: UInt32) -> [Data] { [] }   // TODO(M1): call core
    func onDatagram(_ d: Data) {}                               // TODO(M1)
    func stats() -> Stats { Stats() }                           // TODO(M1)
    func pose(_ t: simd_float4x4, tick: UInt32) -> Data { Data() } // TODO(M1)
}

/// Network.framework UDP client. Receives acks on the same connection.
final class UdpTransport {
    private let conn: NWConnection
    init(host: String, port: UInt16, onReceive: @escaping (Data) -> Void) {
        conn = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port)!, using: .udp)
        conn.start(queue: .global(qos: .userInitiated))
        func loop() {
            conn.receiveMessage { data, _, _, _ in if let data { onReceive(data) }; loop() }
        }
        loop()
    }
    func send(_ d: Data) { conn.send(content: d, completion: .idempotent) }
    func close() { conn.cancel() }
}

/// Per-tick CSV of all tracks for offline evaluation (M7). Exported via the Files app.
final class GroundTruthLog {
    private let handle: FileHandle?
    init() {
        let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("gt-\(Int(Date().timeIntervalSince1970)).csv")
        FileManager.default.createFile(atPath: url.path, contents: "tick,id,class,x,y,z,vx,vy,vz,conf\n".data(using: .utf8))
        handle = try? FileHandle(forWritingTo: url)
    }
    func append(tick: UInt32, tracks: [Track]) {
        guard !tracks.isEmpty else { return }
        var s = ""
        for t in tracks { s += "\(tick),\(t.id),\(t.classId),\(t.pos.x),\(t.pos.y),\(t.pos.z),\(t.vel.x),\(t.vel.y),\(t.vel.z),\(t.conf)\n" }
        handle?.write(s.data(using: .utf8)!)
    }
    func close() { try? handle?.close() }
}

enum DeviceIdentity {
    static let id: UInt32 = {
        if let v = UserDefaults.standard.object(forKey: "deviceId") as? UInt32 { return v }
        let v = UInt32.random(in: 1...0xFFFF); UserDefaults.standard.set(v, forKey: "deviceId"); return v
    }()
}
