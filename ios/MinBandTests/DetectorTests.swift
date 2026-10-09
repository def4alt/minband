import CoreVideo
import ImageIO
import XCTest

final class DetectorTests: XCTestCase {
    func testCocoLabelMappingMatchesCoreClasses() {
        // core/src/classes.rs
        let expected: [String: UInt8] = [
            "person": 0, "backpack": 24, "handbag": 26, "bottle": 39, "cup": 41,
            "chair": 56, "tv": 62, "laptop": 63, "cell phone": 67,
        ]
        for (label, id) in expected {
            XCTAssertEqual(Detector.classId(forLabel: label), id, label)
        }
        XCTAssertEqual(TrackedClass.all.count, 9)
    }

    func testLabelVariantsAndDrops() {
        XCTAssertEqual(Detector.classId(forLabel: "Cell_Phone"), 67)
        XCTAssertEqual(Detector.classId(forLabel: " TV "), 62)
        XCTAssertEqual(Detector.classId(forLabel: "tvmonitor"), 62)
        XCTAssertEqual(Detector.classId(forLabel: "Person"), 0)
        XCTAssertEqual(Detector.classId(forLabel: "67"), 67)   // models exporting bare indices
        XCTAssertEqual(Detector.classId(forLabel: "0"), 0)
        for other in ["car", "dog", "couch", "dining table", "2", "80", "", "mouse"] {
            XCTAssertNil(Detector.classId(forLabel: other), other)
        }
    }

    func testVisionRectToCapturedImageForPortrait() {
        // A box in the raw (landscape) image's top-left corner shows up top-right in portrait.
        // Vision reports it in the oriented image with a bottom-left origin.
        let vision = CGRect(x: 0.9, y: 0.8, width: 0.1, height: 0.2)
        let raw = Detector.capturedImageRect(fromVision: vision, orientation: .right)
        XCTAssertEqual(raw.minX, 0, accuracy: 1e-9)
        XCTAssertEqual(raw.minY, 0, accuracy: 1e-9)
        XCTAssertEqual(raw.width, 0.2, accuracy: 1e-9)   // portrait height -> raw width
        XCTAssertEqual(raw.height, 0.1, accuracy: 1e-9)

        // Feet of an upright person (bottom of the portrait image) are at raw +x.
        let feet = Detector.capturedImageRect(fromVision: CGRect(x: 0.45, y: 0.0, width: 0.1, height: 0.05), orientation: .right)
        XCTAssertEqual(feet.maxX, 1, accuracy: 1e-9)
        XCTAssertEqual(feet.midY, 0.5, accuracy: 1e-9)
    }

    func testVisionRectOtherOrientations() {
        let v = CGRect(x: 0.1, y: 0.2, width: 0.3, height: 0.4)
        let up = Detector.capturedImageRect(fromVision: v, orientation: .up)
        XCTAssertEqual(up.minX, 0.1, accuracy: 1e-9); XCTAssertEqual(up.minY, 0.4, accuracy: 1e-9)
        let down = Detector.capturedImageRect(fromVision: v, orientation: .down)
        XCTAssertEqual(down.minX, 0.6, accuracy: 1e-9); XCTAssertEqual(down.minY, 0.2, accuracy: 1e-9)
        // .left is .right rotated by 180 degrees.
        let l = Detector.capturedImageRect(fromVision: v, orientation: .left)
        let r = Detector.capturedImageRect(fromVision: v, orientation: .right)
        XCTAssertEqual(l.minX, 1 - r.maxX, accuracy: 1e-9)
        XCTAssertEqual(l.minY, 1 - r.maxY, accuracy: 1e-9)
    }

    func testNoModelMeansUnavailableAndEmpty() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let bundle = try XCTUnwrap(Bundle(url: dir))
        XCTAssertTrue(Detector.modelURLs(in: bundle).isEmpty)
        let d = Detector(bundle: bundle)
        XCTAssertFalse(d.isAvailable)
        XCTAssertNotNil(d.lastError)
        XCTAssertEqual(d.detect(pixelBuffer: try makeBuffer(), orientation: .right).count, 0)
    }

    /// Runs only when the (gitignored) YOLO model was present at `xcodegen generate` time, so
    /// it got compiled into the test bundle. Checks loading, the threshold inputs, and a
    /// Vision pass on a blank image.
    func testBundledModelRunsIfPresent() throws {
        let bundle = Bundle(for: DetectorTests.self)
        guard !Detector.modelURLs(in: bundle).isEmpty else { throw XCTSkip("no CoreML model in the test bundle") }
        let d = Detector(bundle: bundle)
        XCTAssertTrue(d.isAvailable, d.lastError ?? "")
        let dets = d.detect(pixelBuffer: try makeBuffer(width: 640, height: 480), orientation: .right)
        XCTAssertTrue(dets.allSatisfy { $0.conf >= Detector.confidenceThreshold && TrackedClass.all.contains($0.classId) })
        XCTAssertNil(d.lastError.flatMap { $0.contains("perform") ? $0 : nil })
    }

    /// End-to-end orientation check on a real photo (developer run, skipped in CI):
    ///   TEST_RUNNER_MINBAND_TEST_IMAGE=/path/upright.jpg \
    ///   TEST_RUNNER_MINBAND_TEST_IMAGE_RAW=/path/upright-rotated-90-ccw.png xcodebuild test ...
    /// The RAW image is what the sensor delivers when the phone is held in portrait. Detecting
    /// it with `.right` must give the same captured-image boxes as detecting the upright photo
    /// with `.up` and rotating the boxes by hand.
    func testRealImageOrientationIfProvided() throws {
        let env = ProcessInfo.processInfo.environment
        guard let upPath = env["MINBAND_TEST_IMAGE"], let rawPath = env["MINBAND_TEST_IMAGE_RAW"] else {
            throw XCTSkip("set MINBAND_TEST_IMAGE and MINBAND_TEST_IMAGE_RAW")
        }
        let d = Detector(bundle: Bundle(for: DetectorTests.self))
        try XCTSkipUnless(d.isAvailable, "no model in the test bundle")
        let viaRaw = d.detect(pixelBuffer: try buffer(path: rawPath), orientation: .right)
        let upright = d.detect(pixelBuffer: try buffer(path: upPath), orientation: .up)
        // Upright (display) normalized -> raw normalized for `.right`: x = y', y = 1 - x'.
        let expected = upright.map { Detection(classId: $0.classId,
            bbox: CGRect(x: $0.bbox.minY, y: 1 - $0.bbox.maxX, width: $0.bbox.height, height: $0.bbox.width),
            conf: $0.conf) }
        let people = viaRaw.filter { $0.classId == TrackedClass.person }
        XCTAssertGreaterThanOrEqual(people.count, 2, "\(viaRaw)")
        for p in people {
            let best = expected.filter { $0.classId == p.classId }.map { iou($0.bbox, p.bbox) }.max() ?? 0
            XCTAssertGreaterThan(best, 0.8, "\(p.bbox) has no upright counterpart")
        }
        // Upright people are taller than wide; in the raw (landscape) image that is wider than tall.
        for p in people { XCTAssertGreaterThan(p.bbox.width * 4 / 3, p.bbox.height, "\(p.bbox)") }
    }

    private func iou(_ a: CGRect, _ b: CGRect) -> CGFloat {
        let i = a.intersection(b)
        guard !i.isNull else { return 0 }
        let ia = i.width * i.height
        return ia / (a.width * a.height + b.width * b.height - ia)
    }

    private func buffer(path: String) throws -> CVPixelBuffer {
        let src = try XCTUnwrap(CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil))
        let img = try XCTUnwrap(CGImageSourceCreateImageAtIndex(src, 0, nil))
        let b = try makeBuffer(width: img.width, height: img.height)
        CVPixelBufferLockBaseAddress(b, [])
        defer { CVPixelBufferUnlockBaseAddress(b, []) }
        let ctx = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(b), width: img.width, height: img.height,
                                          bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(b),
                                          space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                          bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue))
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: img.width, height: img.height))
        return b
    }

    private func makeBuffer(width: Int = 64, height: Int = 48) throws -> CVPixelBuffer {
        var pb: CVPixelBuffer?
        let attrs = [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary
        CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA, attrs, &pb)
        let b = try XCTUnwrap(pb)
        CVPixelBufferLockBaseAddress(b, [])
        memset(CVPixelBufferGetBaseAddress(b), 128, CVPixelBufferGetDataSize(b))
        CVPixelBufferUnlockBaseAddress(b, [])
        return b
    }
}
