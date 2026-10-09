import simd
import XCTest

final class OriginTests: XCTestCase {
    private func assertEqual(_ a: SIMD3<Float>, _ b: SIMD3<Float>, accuracy: Float = 1e-4,
                             file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertLessThan(simd_distance(a, b), accuracy, "\(a) != \(b)", file: file, line: line)
    }

    /// ARImageAnchor-style transform: rotation `r` (columns = image X, normal Y, height Z), origin `o`.
    private func anchor(_ r: simd_quatf, _ o: SIMD3<Float>) -> simd_float4x4 {
        var m = simd_float4x4(r)
        m.columns.3 = SIMD4(o, 1)
        return m
    }

    func testUnlockedIsIdentityAndNotLocked() {
        let o = Origin()
        XCTAssertFalse(o.isLocked)
        assertEqual(o.toMarker([1, 2, 3]), [1, 2, 3])
    }

    func testFlatMarkerWithYaw() {
        let o = Origin()
        let yaw = simd_quatf(angle: 0.7, axis: [0, 1, 0])
        let center = SIMD3<Float>(2, -1.2, -3)
        o.lock(markerTransform: anchor(yaw, center))
        XCTAssertTrue(o.isLocked)
        XCTAssertEqual(o.source, .marker)
        let ax = yaw.act([1, 0, 0]), az = yaw.act([0, 0, 1])
        assertEqual(o.toMarker(center), .zero)
        assertEqual(o.toMarker(center + ax), [1, 0, 0])          // X along marker width
        assertEqual(o.toMarker(center + [0, 1, 0]), [0, 1, 0])   // Y up
        assertEqual(o.toMarker(center + az), [0, 0, 1])          // Z = X x Y, toward bottom edge
        // Right-handed: X x Y = Z.
        assertEqual(simd_cross(SIMD3<Float>(1, 0, 0), [0, 1, 0]), [0, 0, 1])
    }

    func testTiltedMarkerIsLevelledToGravity() {
        let o = Origin()
        // ARKit's estimate of a flat marker is a few degrees off; Y must still be gravity up.
        let r = simd_quatf(angle: 0.3, axis: [0, 1, 0]) * simd_quatf(angle: 4 * .pi / 180, axis: [1, 0, 0])
        let c = SIMD3<Float>(0.5, 0, 0.5)
        o.lock(markerTransform: anchor(r, c))
        assertEqual(o.toMarker(c + [0, 2, 0]), [0, 2, 0])
        let p = o.toMarker(c + simd_quatf(angle: 0.3, axis: [0, 1, 0]).act([3, 0, 0]))
        assertEqual(p, [3, 0, 0], accuracy: 1e-3)
    }

    func testWallMarkerStillYUp() {
        let o = Origin()
        // Image upright on a wall facing +Z: width = +X, normal = +Z, height axis = -Y.
        let r = simd_quatf(angle: .pi / 2, axis: [1, 0, 0])
        XCTAssertLessThan(simd_distance(r.act([0, 1, 0]), [0, 0, 1]), 1e-5)
        o.lock(markerTransform: anchor(r, [0, 1.5, 0]))
        assertEqual(o.toMarker([0, 2.5, 0]), [0, 1, 0])
        assertEqual(o.toMarker([1, 1.5, 0]), [1, 0, 0])
    }

    func testRotationToMarkerMatchesPointTransform() {
        let o = Origin()
        let r = simd_quatf(angle: -1.1, axis: [0, 1, 0])
        o.lock(markerTransform: anchor(r, [4, 0, 1]))
        let q = o.rotationToMarker()
        let v = SIMD3<Float>(0.3, -0.2, 0.9)
        assertEqual(q.act(v), o.toMarkerDirection(v))
        assertEqual(q.act(v), o.toMarker([4, 0, 1] + v))
        // Camera pose composition as EdgeBridge.pose does it: (world->marker) * (camera->world).
        let cam = simd_quatf(angle: 0.4, axis: simd_normalize(SIMD3<Float>(1, 1, 0)))
        let inMarker = q * cam
        assertEqual(inMarker.act([0, 0, -1]), o.toMarkerDirection(cam.act([0, 0, -1])))
        // Full transform helper agrees.
        var t = simd_float4x4(cam); t.columns.3 = SIMD4(1, 1.4, 2, 1)
        let tm = o.toMarker(transform: t)
        assertEqual(SIMD3(tm.columns.3.x, tm.columns.3.y, tm.columns.3.z), o.toMarker([1, 1.4, 2]))
    }

    func testManualLockUsesFloorAndCameraRight() {
        let o = Origin()
        // Portrait, upright, looking along world -Z: camera +X = device bottom (world down),
        // +Y = device right (world +X), +Z = backward (world +Z).
        var cam = simd_float4x4(SIMD4(0, -1, 0, 0), SIMD4(1, 0, 0, 0), SIMD4(0, 0, 1, 0), SIMD4(1, 1.5, 2, 1))
        o.observeHorizontalPlane(y: -0.1, isFloor: true)
        o.lockManual(cameraTransform: cam)
        XCTAssertEqual(o.source, .manual)
        assertEqual(o.toMarker([1, -0.1, 2]), .zero)
        assertEqual(o.toMarker([2, -0.1, 2]), [1, 0, 0])           // camera right = +X
        assertEqual(o.toMarker([1, -0.1, 1]), [0, 0, -1])          // in front of the user = -Z
        XCTAssertEqual(o.floorHeightInMarker ?? 99, 0, accuracy: 1e-5)

        // Looking straight down still gives a level frame.
        let down = simd_quatf(angle: -.pi / 2, axis: [1, 0, 0])   // pitch the upright pose down
        let r = down * simd_quatf(simd_float3x3(SIMD3(0, -1, 0), SIMD3(1, 0, 0), SIMD3(0, 0, 1)))
        cam = simd_float4x4(r); cam.columns.3 = SIMD4(0, 1.2, 0, 1)
        let o2 = Origin()
        o2.lockManual(cameraTransform: cam)  // no floor plane: default camera height
        assertEqual(o2.toMarker([0, 1.2 - Origin.defaultCameraHeight, 0]), .zero)
        assertEqual(o2.toMarker([0, 2.2 - Origin.defaultCameraHeight, 0]), [0, 1, 0])
    }

    func testDifferenceAndRelockChange() {
        let o = Origin()
        let a = anchor(simd_quatf(angle: 0, axis: [0, 1, 0]), [0, 0, 0])
        let first = o.lock(markerTransform: a)
        XCTAssertFalse(first.wasLocked)
        let b = anchor(simd_quatf(angle: 2 * .pi / 180, axis: [0, 1, 0]), [0.03, 0, 0])
        let d = o.difference(markerTransform: b)!
        XCTAssertEqual(d.translation, 0.03, accuracy: 1e-5)
        XCTAssertEqual(d.angle, 2 * .pi / 180, accuracy: 1e-3)
        let second = o.lock(markerTransform: b)
        XCTAssertTrue(second.wasLocked)
        XCTAssertEqual(second.previousSource, .marker)
        o.reset()
        XCTAssertFalse(o.isLocked)
        XCTAssertNil(o.difference(markerTransform: b))
    }

    func testFloorPrefersClassifiedPlane() {
        let o = Origin()
        o.observeHorizontalPlane(y: -1.6, isFloor: false)
        XCTAssertEqual(o.floorY, -1.6)
        o.observeHorizontalPlane(y: -1.4, isFloor: true)
        XCTAssertEqual(o.floorY, -1.4)
    }
}
