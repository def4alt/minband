import Foundation
import simd

/// Swift face of the Rust core (`core/src/ffi.rs`; bindings in `Generated/minband_core.swift`,
/// C module `minband_coreFFI` and static lib in `Frameworks/MinBandCore.xcframework`, both from
/// `tools/build-ios.sh`). All sync logic (ghosts, thresholds, budget controller, codec) runs in
/// Rust; this class converts types and keeps a bytes/s estimate for the HUD.
///
/// Thread-safe: `tick`/`stats`/`pose` run on the ARKit delegate queue and `onDatagram` on the UDP
/// receive queue. The Rust object locks internally; the sample ring has its own lock.
final class EdgeBridge {
    struct Stats { var seq: UInt32 = 0; var thetaScale: Double = 1; var bytesPerSecEstimate: Int = 0 }

    private static let ticksPerSecond: UInt32 = 120   // core TICK_HZ
    private static let rateWindow: UInt32 = 2 * ticksPerSecond
    private static let maxSamples = 256

    private let core: FfiEdge
    private let lock = NSLock()
    /// (edge tick, core bytesTotal) after every `tick`, oldest first. `samples[0]` is the newest
    /// sample at least `rateWindow` old, so the estimate always spans ~2 s once warmed up.
    private var samples: [(tick: UInt32, bytes: UInt64)] = []

    init(deviceId: UInt32, sessionNonce: UInt32) {
        core = FfiEdge(deviceId: deviceId, sessionNonce: sessionNonce)
    }

    /// Feed the tracker output at edge tick `now` (1/120 s since session start). Returns the
    /// datagrams to send right now (Hello until the server acks, then Delta/Keyframe).
    func tick(tracks: [Track], now: UInt32) -> [Data] {
        let out = core.tick(tracks: tracks.map(Self.ffiTrack), now: now)
        record(tick: now, bytes: core.stats().bytesTotal)
        return out
    }

    /// Datagram from the server (Ack: last seq, missing seqs for state repair, byte budget).
    func onDatagram(_ d: Data) { core.onDatagram(bytes: d) }

    func stats() -> Stats {
        let s = core.stats()
        return Stats(seq: s.seq, thetaScale: Double(s.thetaScale), bytesPerSecEstimate: bytesPerSec())
    }

    /// `Pose` datagram for `ARCamera.transform` (camera -> ARKit world). Position is the camera
    /// centre in the marker frame; orientation is a unit quaternion `[x, y, z, w]` (w last,
    /// w >= 0) rotating ARKit camera-frame vectors into the marker frame. ARKit camera axes are
    /// those of the landscape-right sensor: +X right, +Y up, -Z along the optical axis (in portrait
    /// +X is the device's bottom edge, +Y its right edge). Consumes a seq. Returns empty `Data`
    /// until the server has acked (the edge sends only Hello before that); do not send it.
    func pose(_ t: simd_float4x4, tick: UInt32) -> Data {
        let origin = Origin.shared
        let p = origin.toMarker(SIMD3(t.columns.3.x, t.columns.3.y, t.columns.3.z))
        // (world -> marker) * (camera -> world) = camera -> marker.
        let q = (origin.rotationToMarker() * simd_quatf(t)).normalized
        var v = q.vector   // (ix, iy, iz, r) = (x, y, z, w)
        if v.w < 0 { v = -v }   // q and -q are the same rotation; keep the wire canonical
        return core.encodePose(pos: [p.x, p.y, p.z], quat: [v.x, v.y, v.z, v.w],
                               originLocked: origin.isLocked, tick: tick)
    }

    // MARK: - Private

    private static func ffiTrack(_ t: Track) -> FfiTrack {
        FfiTrack(id: t.id, class: t.classId, pos: [t.pos.x, t.pos.y, t.pos.z],
                 vel: [t.vel.x, t.vel.y, t.vel.z], conf: t.conf)
    }

    private func record(tick: UInt32, bytes: UInt64) {
        lock.lock(); defer { lock.unlock() }
        // Clock went backwards (ticks are u32 and wrap-compared like the core): start over.
        if let last = samples.last, tick &- last.tick > UInt32.max / 2 { samples.removeAll() }
        samples.append((tick, bytes))
        while samples.count > 2, tick &- samples[1].tick >= Self.rateWindow { samples.removeFirst() }
        if samples.count > Self.maxSamples { samples.removeFirst(samples.count - Self.maxSamples) }
    }

    /// Payload bytes/s over the last ~2 s of edge ticks (UDP/IP headers not included).
    private func bytesPerSec() -> Int {
        lock.lock(); defer { lock.unlock() }
        guard let first = samples.first, let last = samples.last, last.tick != first.tick else { return 0 }
        let seconds = Double(last.tick &- first.tick) / Double(Self.ticksPerSecond)
        return Int((Double(last.bytes &- first.bytes) / seconds).rounded())
    }
}
