import simd
import XCTest

final class TrackerTests: XCTestCase {
    private let dt = 1.0 / 12.0

    private func point(_ p: SIMD3<Float>, cls: UInt8 = TrackedClass.person, conf: Float = 0.8) -> WorldPoint {
        WorldPoint(classId: cls, pos: p, conf: conf)
    }

    func testBirthAfterThreeHits() {
        let tr = Tracker()
        tr.update([point([1, 0, 2])], time: 0)
        XCTAssertTrue(tr.tracks(at: 0).isEmpty)
        tr.update([point([1, 0, 2])], time: dt)
        XCTAssertTrue(tr.tracks(at: dt).isEmpty, "still tentative after 2 hits")
        let ids = tr.update([point([1, 0, 2])], time: 2 * dt)
        let tracks = tr.tracks(at: 2 * dt)
        XCTAssertEqual(tracks.count, 1)
        XCTAssertEqual(tracks.first?.id, 1)
        XCTAssertEqual(tracks.first?.classId, TrackedClass.person)
        XCTAssertEqual(ids, [1], "update reports the confirmed id for the overlay")
        XCTAssertEqual(tracks.first!.pos.x, 1, accuracy: 1e-3)
    }

    func testDeathAfterOneSecondWithoutHits() {
        let tr = Tracker()
        for i in 0..<5 { tr.update([point([0, 0, 1], cls: TrackedClass.chair)], time: Double(i) * dt) }
        let last = 4 * dt
        XCTAssertEqual(tr.tracks(at: last + 0.95).count, 1, "coasts for up to 1 s")
        XCTAssertEqual(tr.tracks(at: last + 1.05).count, 0, "dead after 1 s without hits")
        // Updates without matching points also prune.
        tr.update([], time: last + 2)
        XCTAssertEqual(tr.confirmedCount, 0)
    }

    func testTentativeTracksDieQuickly() {
        let tr = Tracker()
        tr.update([point([3, 0, 3])], time: 0)
        tr.update([], time: 0.5)
        tr.update([point([3, 0, 3])], time: 0.6)
        tr.update([point([3, 0, 3])], time: 0.7)
        XCTAssertTrue(tr.tracks(at: 0.7).isEmpty, "the first hit expired, so only 2 hits count")
    }

    func testIdsMonotonicAndNeverReused() {
        let tr = Tracker()
        for i in 0..<3 { tr.update([point([0, 0, 0]), point([5, 0, 0])], time: Double(i) * dt) }
        XCTAssertEqual(tr.tracks(at: 2 * dt).map(\.id), [1, 2])
        tr.reset()
        let t0 = 10.0
        for i in 0..<3 { tr.update([point([0, 0, 0])], time: t0 + Double(i) * dt) }
        XCTAssertEqual(tr.tracks(at: t0 + 2 * dt).map(\.id), [3], "ids continue after reset/death")
    }

    func testConstantVelocityEstimateWithin10Percent() {
        let tr = Tracker()
        let v = SIMD3<Float>(0.9, 0, -0.8)      // ~1.2 m/s walker
        let p0 = SIMD3<Float>(-1, 0, 2)
        var rng = SplitMix(seed: 7)
        var tail: [SIMD3<Float>] = []
        let n = 48                               // 4 s at 12 Hz
        for i in 0..<n {
            let t = Double(i) * dt
            let noise = SIMD3<Float>(rng.gauss(), rng.gauss(), rng.gauss()) * 0.03
            tr.update([point(p0 + v * Float(t) + noise)], time: t)
            if i >= n - 12, let tk = tr.tracks(at: t).first { tail.append(tk.vel) }
        }
        let mean = tail.reduce(.zero, +) / Float(tail.count)
        let final = tr.tracks(at: Double(n - 1) * dt).first!.vel
        XCTAssertLessThan(simd_length(mean - v) / simd_length(v), 0.10, "mean over last second: \(mean)")
        XCTAssertLessThan(simd_length(final - v) / simd_length(v), 0.10, "final estimate: \(final)")

        // Noise-free target: essentially exact.
        let clean = Tracker()
        for i in 0..<n { clean.update([point(p0 + v * Float(Double(i) * dt))], time: Double(i) * dt) }
        let vc = clean.tracks(at: Double(n - 1) * dt).first!.vel
        XCTAssertLessThan(simd_length(vc - v) / simd_length(v), 0.02, "\(vc)")
    }

    func testPredictionExtrapolatesBetweenDetections() {
        let tr = Tracker()
        let v = SIMD3<Float>(1, 0, 0)
        for i in 0..<36 { tr.update([point(v * Float(Double(i) * dt))], time: Double(i) * dt) }
        let tLast = 35 * dt
        let a = tr.tracks(at: tLast).first!.pos
        let b = tr.tracks(at: tLast + 0.5).first!.pos
        XCTAssertEqual(b.x - a.x, 0.5, accuracy: 0.05)
    }

    func testVelocityClampedToClassMaxSpeed() {
        let fast = SIMD3<Float>(2.0, 0, 0)
        let chair = Tracker(), person = Tracker()
        for i in 0..<24 {
            let t = Double(i) * dt
            chair.update([point(fast * Float(t), cls: TrackedClass.chair)], time: t)
            person.update([point(fast * Float(t), cls: TrackedClass.person)], time: t)
        }
        let tq = 23 * dt
        // A static class moving at 2 m/s: the 0.4 m gate still holds (0.17 m per frame).
        XCTAssertLessThanOrEqual(simd_length(chair.tracks(at: tq).first!.vel), 1.0 + 1e-5)
        XCTAssertEqual(simd_length(person.tracks(at: tq).first!.vel), 2.0, accuracy: 0.2)
    }

    func testPerClassGating() {
        let tr = Tracker()
        for i in 0..<3 {
            tr.update([point([0, 0, 0], cls: TrackedClass.person), point([3, 0, 0], cls: TrackedClass.cup)], time: Double(i) * dt)
        }
        let t = 3 * dt
        // Person jumps 0.6 m (inside the 0.7 m gate); cup jumps 0.6 m (outside its 0.4 m gate).
        tr.update([point([0.6, 0, 0], cls: TrackedClass.person), point([3.6, 0, 0], cls: TrackedClass.cup)], time: t)
        let tracks = tr.tracks(at: t)
        XCTAssertEqual(tracks.count, 2)
        let person = tracks.first { $0.classId == TrackedClass.person }!
        let cup = tracks.first { $0.classId == TrackedClass.cup }!
        XCTAssertGreaterThan(person.pos.x, 0.2, "person associated and moved")
        XCTAssertEqual(cup.pos.x, 3, accuracy: 0.05, "cup did not associate; a new tentative track was born")
        // Different classes never associate.
        let other = Tracker()
        for i in 0..<3 { other.update([point([0, 0, 0], cls: TrackedClass.cup)], time: Double(i) * dt) }
        other.update([point([0.01, 0, 0], cls: TrackedClass.bottle)], time: 3 * dt)
        XCTAssertEqual(other.tracks(at: 3 * dt).map(\.classId), [TrackedClass.cup])
    }

    func testNearestNeighbourPrefersClosest() {
        let tr = Tracker()
        for i in 0..<3 { tr.update([point([0, 0, 0]), point([1, 0, 0])], time: Double(i) * dt) }
        // Points arrive in swapped order; association must still be by distance.
        tr.update([point([1.05, 0, 0]), point([0.05, 0, 0])], time: 3 * dt)
        let tracks = tr.tracks(at: 3 * dt)
        XCTAssertEqual(tracks.count, 2)
        XCTAssertLessThan(tracks[0].pos.x, 0.2)   // id 1 stayed near 0
        XCTAssertGreaterThan(tracks[1].pos.x, 0.9) // id 2 stayed near 1
    }

    func testConfidenceIsSmoothedAndStable() {
        let tr = Tracker()
        var confs: Set<UInt8> = []
        for i in 0..<60 {
            let c: Float = i % 2 == 0 ? 0.70 : 0.76   // flickering detector score
            tr.update([point([0, 0, 0], conf: c)], time: Double(i) * dt)
            if let tk = tr.tracks(at: Double(i) * dt).first { confs.insert(tk.conf) }
        }
        XCTAssertLessThanOrEqual(confs.count, 2, "conf output must not flicker: \(confs)")
    }
}

/// Deterministic Gaussian noise for tests.
struct SplitMix {
    var state: UInt64
    init(seed: UInt64) { state = seed }
    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }
    mutating func uniform() -> Double { Double(next() >> 11) / Double(1 << 53) }
    mutating func gauss() -> Float {
        let u1 = max(uniform(), 1e-12), u2 = uniform()
        return Float((-2 * log(u1)).squareRoot() * cos(2 * Double.pi * u2))
    }
}
