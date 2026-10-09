import Foundation

/// Per-tick CSV of all tracks for offline evaluation (M7). Exported via the Files app
/// (UIFileSharingEnabled) or the "share log" button.
///
/// Format (tools/eval/src/gt.ts depends on it): header `tick,id,class,x,y,z,vx,vy,vz,conf`, one row
/// per track per logged frame, tick = edge clock (1/120 s since session start), marker frame.
/// Rows are buffered in memory and written to disk at most once per second (and on close).
final class GroundTruthLog {
    static let header = "tick,id,class,x,y,z,vx,vy,vz,conf\n"
    static let flushInterval: TimeInterval = 1.0

    let url: URL
    private let handle: FileHandle?
    private let io = DispatchQueue(label: "minband.gtlog", qos: .utility)
    private var buffer = ""
    private var lastFlush = Date()
    private let lock = NSLock()

    init(directory: URL = GroundTruthLog.documentsDirectory, name: String? = nil) {
        url = directory.appendingPathComponent(name ?? "gt-\(Int(Date().timeIntervalSince1970)).csv")
        FileManager.default.createFile(atPath: url.path, contents: Data(GroundTruthLog.header.utf8))
        handle = try? FileHandle(forWritingTo: url)
        _ = try? handle?.seekToEnd()
    }

    deinit { close() }

    func append(tick: UInt32, tracks: [Track], now: Date = Date()) {
        guard !tracks.isEmpty else { return }
        var s = ""
        for t in tracks { s += GroundTruthLog.row(tick: tick, track: t) }
        let pending: String? = lock.withLock {
            buffer += s
            guard now.timeIntervalSince(lastFlush) >= GroundTruthLog.flushInterval else { return nil }
            lastFlush = now
            defer { buffer = "" }
            return buffer
        }
        if let pending { write(pending) }
    }

    /// Writes everything buffered so far and waits for it to hit the file.
    func flush() {
        let pending: String = lock.withLock { defer { buffer = "" }; lastFlush = Date(); return buffer }
        if !pending.isEmpty { write(pending) }
        io.sync {}
    }

    func close() {
        flush()
        io.sync { try? handle?.synchronize(); try? handle?.close() }
    }

    static func row(tick: UInt32, track t: Track) -> String {
        func f(_ v: Float) -> String { String(format: "%.5f", v) }
        return "\(tick),\(t.id),\(t.classId),\(f(t.pos.x)),\(f(t.pos.y)),\(f(t.pos.z)),\(f(t.vel.x)),\(f(t.vel.y)),\(f(t.vel.z)),\(t.conf)\n"
    }

    private func write(_ s: String) {
        let data = Data(s.utf8)
        io.async { [handle] in try? handle?.write(contentsOf: data) }
    }

    // MARK: files

    static var documentsDirectory: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    /// Most recent `gt-*.csv` in Documents (by modification date).
    static func latest(in directory: URL = documentsDirectory) -> URL? {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.contentModificationDateKey]) else { return nil }
        return files
            .filter { $0.lastPathComponent.hasPrefix("gt-") && $0.pathExtension == "csv" }
            .max { a, b in
                let da = (try? a.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                let db = (try? b.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                return (da, a.lastPathComponent) < (db, b.lastPathComponent)
            }
    }
}

enum DeviceIdentity {
    static let id: UInt32 = {
        if let v = UserDefaults.standard.object(forKey: "deviceId") as? UInt32 { return v }
        let v = UInt32.random(in: 1...0xFFFF); UserDefaults.standard.set(v, forKey: "deviceId"); return v
    }()
}
