import Foundation
import simd

/// 3D nearest-neighbour association + constant-velocity Kalman per track. Birth after 3 hits,
/// death after 1 s. Velocity quality here decides how often the core sends deltas.
///
/// Pure Swift (no ARKit): input is `WorldPoint`s in the marker frame, output `Track`s.
///
/// Filter: state x = [p, v] (6D). Process model is continuous white-noise acceleration with
/// spectral density `q` (m^2/s^3), measurement z = p + noise(sigma). Because F, H, Q and R are the
/// same for all three axes and the noise is isotropic, the 6x6 covariance is block-diagonal with
/// three identical 2x2 blocks, so one 2x2 covariance per track is the exact filter.
///
/// Thread safety: `update` runs on the detection queue, `tracks(at:)` on the AR delegate queue;
/// an internal lock serialises them.
final class Tracker {
    struct Config {
        /// Hits needed before a track is reported (and gets an id).
        var birthHits = 3
        /// Confirmed tracks die after this long without a hit.
        var deathAfter: TimeInterval = 1.0
        /// Tentative tracks die faster so clutter does not accumulate.
        var tentativeDeathAfter: TimeInterval = 0.35
        /// Measurement noise (depth ~0.1 m).
        var measurementSigma: Float = 0.1
        /// Initial velocity std for a new track.
        var initialVelocitySigma: Float = 1.5
        /// CWNA process noise per class (m^2/s^3). Tuned in simulation at 12 Hz, sigma 0.1 m:
        /// q = 0.1 gives ~0.1 m/s per-axis velocity noise (well under the core's 0.3 m/s
        /// theta_vel) and follows a 90 degree turn at 1 m/s within ~0.8 s with <0.25 m lag.
        /// Placed objects (chair, laptop, tv) barely move, so they get a much smoother filter.
        var processNoise: (UInt8) -> Float = { cls in
            switch cls {
            case TrackedClass.chair, TrackedClass.laptop, TrackedClass.tv: return 0.02
            default: return 0.1
            }
        }
        /// Detection confidence smoothing (EMA weight of a new hit) and the change in 0..255
        /// units below which the reported conf does not move (stops core conf-bucket flapping).
        var confAlpha: Float = 0.2
        var confHysteresis: Int = 12
    }

    private struct State {
        var p: SIMD3<Float>
        var v: SIMD3<Float>
        // 2x2 covariance shared by the three axes.
        var ppp: Float
        var ppv: Float
        var pvv: Float
        var time: TimeInterval
    }

    private struct Entry {
        let classId: UInt8
        var id: UInt32?          // assigned at birth
        var hits: Int
        var lastHit: TimeInterval
        var s: State
        var confEMA: Float       // 0..1
        var confOut: UInt8
    }

    let config: Config
    private var entries: [Entry] = []
    private var nextId: UInt32 = 1
    private let lock = NSLock()

    init(config: Config = Config()) { self.config = config }

    /// Number of confirmed tracks alive right now (any time).
    var confirmedCount: Int { lock.withLock { entries.filter { $0.id != nil }.count } }

    /// Drops every track but keeps the id counter, so ids are never reused within a session.
    func reset() { lock.withLock { entries.removeAll() } }

    /// Associate `points` (marker frame, observed at `time`) with tracks and run the KF update.
    /// Returns, per input point, the id of the confirmed track it updated (nil for tentative or
    /// newly created tracks).
    @discardableResult
    func update(_ points: [WorldPoint], time: TimeInterval) -> [UInt32?] {
        lock.lock(); defer { lock.unlock() }
        prune(at: time)

        // Predict every track to `time` (only forward; a late measurement is applied at the
        // track's own time).
        for i in entries.indices where time > entries[i].s.time {
            predict(&entries[i].s, to: time, q: config.processNoise(entries[i].classId))
        }

        // Greedy global nearest neighbour inside per-class gates.
        var pairs: [(d: Float, t: Int, m: Int)] = []
        for (m, pt) in points.enumerated() {
            let gate = TrackedClass.gate(pt.classId)
            for (t, e) in entries.enumerated() where e.classId == pt.classId {
                let d = simd_distance(e.s.p, pt.pos)
                if d <= gate { pairs.append((d, t, m)) }
            }
        }
        pairs.sort { $0.d < $1.d || ($0.d == $1.d && ($0.t, $0.m) < ($1.t, $1.m)) }
        var trackUsed = [Bool](repeating: false, count: entries.count)
        var pointTrack = [Int?](repeating: nil, count: points.count)
        for pr in pairs where !trackUsed[pr.t] && pointTrack[pr.m] == nil {
            trackUsed[pr.t] = true; pointTrack[pr.m] = pr.t
        }

        var result = [UInt32?](repeating: nil, count: points.count)
        for (m, pt) in points.enumerated() {
            if let t = pointTrack[m] {
                correct(&entries[t].s, z: pt.pos)
                entries[t].hits += 1
                entries[t].lastHit = max(entries[t].lastHit, time)
                updateConf(&entries[t], conf: pt.conf)
                if entries[t].id == nil, entries[t].hits >= config.birthHits {
                    entries[t].id = nextId; nextId &+= 1
                }
                result[m] = entries[t].id
            } else {
                let r = config.measurementSigma * config.measurementSigma
                let vs = config.initialVelocitySigma
                let c = max(0, min(1, pt.conf))
                var e = Entry(classId: pt.classId, id: nil, hits: 1, lastHit: time,
                              s: State(p: pt.pos, v: .zero, ppp: r, ppv: 0, pvv: vs * vs, time: time),
                              confEMA: c, confOut: UInt8((c * 255).rounded()))
                if config.birthHits <= 1 { e.id = nextId; nextId &+= 1; result[m] = e.id }
                entries.append(e)
            }
        }
        return result
    }

    /// Confirmed tracks predicted to `time` (not mutating the filters). Velocity is clamped to
    /// the class max speed and the clamped velocity is what the position is extrapolated with.
    func tracks(at time: TimeInterval) -> [Track] {
        lock.lock(); defer { lock.unlock() }
        prune(at: time)
        var out: [Track] = []
        out.reserveCapacity(entries.count)
        for e in entries {
            guard let id = e.id else { continue }
            let vmax = TrackedClass.maxSpeed(e.classId)
            var v = e.s.v
            let speed = simd_length(v)
            if speed > vmax { v *= vmax / speed }
            let dt = Float(max(0, time - e.s.time))
            out.append(Track(id: id, classId: e.classId, pos: e.s.p + v * dt, vel: v, conf: e.confOut))
        }
        out.sort { $0.id < $1.id }
        return out
    }

    // MARK: internals (lock held)

    private func prune(at time: TimeInterval) {
        entries.removeAll { e in
            let silent = time - e.lastHit
            return e.id == nil ? silent > config.tentativeDeathAfter : silent > config.deathAfter
        }
    }

    private func predict(_ s: inout State, to time: TimeInterval, q: Float) {
        let dt = Float(time - s.time)
        guard dt > 0 else { return }
        s.p += s.v * dt
        let dt2 = dt * dt, dt3 = dt2 * dt
        let ppp = s.ppp + 2 * dt * s.ppv + dt2 * s.pvv + q * dt3 / 3
        let ppv = s.ppv + dt * s.pvv + q * dt2 / 2
        let pvv = s.pvv + q * dt
        s.ppp = ppp; s.ppv = ppv; s.pvv = pvv
        s.time = time
    }

    private func correct(_ s: inout State, z: SIMD3<Float>) {
        let r = config.measurementSigma * config.measurementSigma
        let sInv = 1 / (s.ppp + r)
        let kp = s.ppp * sInv, kv = s.ppv * sInv
        let y = z - s.p
        s.p += kp * y
        s.v += kv * y
        let ppp = (1 - kp) * s.ppp
        let ppv = (1 - kp) * s.ppv
        let pvv = s.pvv - kv * s.ppv
        s.ppp = ppp; s.ppv = ppv; s.pvv = max(pvv, 1e-6)
    }

    private func updateConf(_ e: inout Entry, conf: Float) {
        let c = max(0, min(1, conf))
        e.confEMA += config.confAlpha * (c - e.confEMA)
        let target = Int((e.confEMA * 255).rounded())
        if abs(target - Int(e.confOut)) >= config.confHysteresis { e.confOut = UInt8(target) }
    }
}
