import simd
import XCTest

final class Lift3DTests: XCTestCase {
    private let res = SIMD2<Float>(1920, 1440)
    private let k = simd_float3x3(SIMD3(1500, 0, 0), SIMD3(0, 1500, 0), SIMD3(960, 720, 1))

    func testUnprojectPrincipalPointIsOnOpticalAxis() {
        let p = Lift3D.unproject(normalized: CGPoint(x: 0.5, y: 0.5), depth: 2, intrinsics: k, imageResolution: res)
        XCTAssertLessThan(simd_distance(p, [0, 0, -2]), 1e-5)   // ARKit camera looks down -Z
    }

    func testUnprojectOffAxis() {
        // 300 px right and 150 px down of the principal point at 3 m.
        let pt = CGPoint(x: Double(960 + 300) / 1920, y: Double(720 + 150) / 1440)
        let p = Lift3D.unproject(normalized: pt, depth: 3, intrinsics: k, imageResolution: res)
        XCTAssertEqual(p.x, 300.0 / 1500 * 3, accuracy: 1e-4)
        XCTAssertEqual(p.y, -150.0 / 1500 * 3, accuracy: 1e-4)  // image down = camera -Y
        XCTAssertEqual(p.z, -3, accuracy: 1e-6)
    }

    func testMedianRejectsOutliers() {
        var v: [Float] = [2.0, 2.1, 1.9, 9.0, 0.2, 2.05, 1.95]
        XCTAssertEqual(Lift3D.median(&v)!, 2.0, accuracy: 1e-6)
        var even: [Float] = [1, 2, 3, 4]
        XCTAssertEqual(Lift3D.median(&even)!, 2.5)
        var none: [Float] = []
        XCTAssertNil(Lift3D.median(&none))
    }

    func testGravityDownInPortraitIsRawPlusX() {
        // Upright portrait pose: camera +X = world down.
        let cam = simd_float4x4(SIMD4(0, -1, 0, 0), SIMD4(1, 0, 0, 0), SIMD4(0, 0, 1, 0), SIMD4(0, 1.4, 0, 1))
        let d = Lift3D.downDirectionInImage(cameraTransform: cam)
        XCTAssertEqual(d.x, 1, accuracy: 1e-5)
        XCTAssertEqual(d.y, 0, accuracy: 1e-5)
        // Landscape-right (camera axes = world axes): down is image +y.
        let d2 = Lift3D.downDirectionInImage(cameraTransform: matrix_identity_float4x4)
        XCTAssertEqual(d2.y, 1, accuracy: 1e-5)
    }

    func testBottomPointOfPersonBox() {
        let box = CGRect(x: 0.3, y: 0.4, width: 0.4, height: 0.2)
        let p = Lift3D.bottomPoint(of: box, down: [1, 0], imageResolution: res)
        XCTAssertEqual(p.y, 0.5, accuracy: 1e-6)
        XCTAssertGreaterThan(p.x, 0.68); XCTAssertLessThanOrEqual(p.x, 0.7)
    }

    func testGroundTruthRowFormat() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let log = GroundTruthLog(directory: dir, name: "gt-1.csv")
        let t = Track(id: 7, classId: 0, pos: [1, 0, -2.5], vel: [0.25, 0, -1], conf: 200)
        log.append(tick: 120, tracks: [t])
        log.append(tick: 124, tracks: [])
        log.close()
        let text = try String(contentsOf: log.url, encoding: .utf8)
        XCTAssertEqual(text, "tick,id,class,x,y,z,vx,vy,vz,conf\n120,7,0,1.00000,0.00000,-2.50000,0.25000,0.00000,-1.00000,200\n")
        XCTAssertEqual(GroundTruthLog.latest(in: dir)?.lastPathComponent, "gt-1.csv")
    }
}
