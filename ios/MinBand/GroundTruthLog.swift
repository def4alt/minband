import ARKit
import Foundation
import Network

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
