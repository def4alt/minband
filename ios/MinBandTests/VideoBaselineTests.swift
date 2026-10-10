import XCTest

/// The pure half of `VideoBaseline`: bps arithmetic, file names and the JSON tools/eval reads
/// (`loadBaselineA` in tools/eval/src/baselines.ts). The encoders themselves need a device.
final class VideoBaselineTests: XCTestCase {
    private let r720 = VideoBaseline.renditions[0]
    private let r480 = VideoBaseline.renditions[1]

    func testRenditionsMatchToolsEvalIds() {
        let r = VideoBaseline.renditions
        XCTAssertEqual(r.map(\.id), ["h264_720p", "h264_480p", "h264_360p"])
        XCTAssertEqual(r.map(\.label), ["H.264 720p", "H.264 480p", "H.264 360p"])
        XCTAssertEqual(r.map(\.resolution), ["1280x720", "854x480", "640x360"])
        XCTAssertEqual(r.map(\.targetBps), [1_500_000, 500_000, 250_000])
        XCTAssertEqual(r.map { VideoBaseline.rateText($0.targetBps) }, ["1.5 Mbps", "500 kbps", "250 kbps"])
    }

    func testBitsPerSecond() {
        XCTAssertEqual(VideoBaseline.bitsPerSecond(bytes: 187_500, seconds: 1), 1_500_000)
        XCTAssertEqual(VideoBaseline.bitsPerSecond(bytes: 1_000, seconds: 3), 2_667)   // 2666.67 rounded
        XCTAssertNil(VideoBaseline.bitsPerSecond(bytes: 0, seconds: 10))
        XCTAssertNil(VideoBaseline.bitsPerSecond(bytes: 1_000, seconds: 0))
        XCTAssertNil(VideoBaseline.bitsPerSecond(bytes: 1_000, seconds: .infinity))
    }

    func testDurationCoversTheLastFrame() {
        // 31 frames at 30 Hz, presented from 0 s to 1 s, cover 31/30 s.
        XCTAssertEqual(VideoBaseline.duration(first: 0, last: 1), 31.0 / 30, accuracy: 1e-9)
        XCTAssertEqual(VideoBaseline.duration(first: 2, last: 2), 1.0 / 30, accuracy: 1e-9)
    }

    func testFilesSitNextToTheGroundTruthLog() {
        let log = URL(fileURLWithPath: "/tmp/docs/gt-1712345.csv")
        let json = VideoBaseline.reportURL(forLog: log)
        XCTAssertEqual(json.lastPathComponent, "baseline_a-1712345.json")
        XCTAssertEqual(json.deletingLastPathComponent().path, "/tmp/docs")
        XCTAssertEqual(VideoBaseline.videoURL(forLog: log, rendition: r720).lastPathComponent, "h264_720p-1712345.mp4")
        XCTAssertEqual(VideoBaseline.sessionId(forLog: URL(fileURLWithPath: "/tmp/run.csv")), "run")
    }

    func testEntry() throws {
        // 11.25 MB of H.264 samples over 62.4 s = 1 442 307.7 bit/s.
        let m = VideoBaseline.Measurement(rendition: r720, bytes: 11_250_000, seconds: 62.4, frames: 1872)
        let e = try XCTUnwrap(VideoBaseline.entry(m, session: "gt-1712345"))
        XCTAssertEqual(e.id, "h264_720p")
        XCTAssertEqual(e.bps, 1_442_308)
        XCTAssertEqual(e.label, "H.264 720p")
        XCTAssertEqual(e.resolution, "1280x720")
        XCTAssertEqual(e.source, "measured: AVAssetWriter H.264 720p30 target 1.5 Mbps, session gt-1712345, 62.4 s")

        let dropped = VideoBaseline.Measurement(rendition: r480, bytes: 3_900_000, seconds: 62.4,
                                                frames: 1870, dropped: 2, fromFileSize: true)
        XCTAssertEqual(VideoBaseline.entry(dropped, session: "gt-1712345")?.source,
                       "measured: AVAssetWriter H.264 480p30 target 500 kbps, session gt-1712345, 62.4 s, "
                       + "2 of 1872 frames dropped, file size incl. container")

        // Nothing encoded: no entry at all (tools/eval rejects bps <= 0).
        XCTAssertNil(VideoBaseline.entry(VideoBaseline.Measurement(rendition: r480, bytes: 0, seconds: 10), session: "s"))
    }

    func testJSONHasTheShapeToolsEvalReads() throws {
        let a = try XCTUnwrap(VideoBaseline.entry(
            VideoBaseline.Measurement(rendition: r720, bytes: 11_250_000, seconds: 62.4), session: "gt-1712345"))
        let b = try XCTUnwrap(VideoBaseline.entry(
            VideoBaseline.Measurement(rendition: r480, bytes: 3_900_000, seconds: 62.4), session: "gt-1712345"))
        let data = try VideoBaseline.reportJSON([a, b])

        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Array(root.keys), ["entries"])
        let entries = try XCTUnwrap(root["entries"] as? [[String: Any]])
        XCTAssertEqual(entries.count, 2)
        XCTAssertEqual(Set(entries[0].keys), ["id", "bps", "label", "resolution", "source"])
        XCTAssertEqual(entries[0]["id"] as? String, "h264_720p")
        XCTAssertEqual(entries[0]["bps"] as? Int, 1_442_308)
        XCTAssertEqual(entries[0]["label"] as? String, "H.264 720p")
        XCTAssertEqual(entries[0]["resolution"] as? String, "1280x720")
        XCTAssertEqual(entries[0]["source"] as? String, a.source)
        XCTAssertEqual(entries[1]["id"] as? String, "h264_480p")
        XCTAssertEqual(entries[1]["bps"] as? Int, 500_000)
        // bps is a JSON integer, not 1442308.0.
        XCTAssertFalse(String(decoding: data, as: UTF8.self).contains("1442308."))

        let back = try JSONDecoder().decode(VideoBaseline.Report.self, from: data)
        XCTAssertEqual(back.entries, [a, b])
    }

    func testSummary() throws {
        let a = try XCTUnwrap(VideoBaseline.entry(
            VideoBaseline.Measurement(rendition: r720, bytes: 11_250_000, seconds: 62.4), session: "s"))
        let b = try XCTUnwrap(VideoBaseline.entry(
            VideoBaseline.Measurement(rendition: r480, bytes: 3_900_000, seconds: 62.4), session: "s"))
        XCTAssertEqual(VideoBaseline.summary([a, b]), "h.264 kbps  720p 1442 · 480p 500")

        var outcome = VideoBaseline.Outcome()
        outcome.problems = ["360p: no frames"]
        XCTAssertEqual(outcome.summary, "h.264 baseline failed: 360p: no frames")
        outcome.entries = [a, b]
        XCTAssertEqual(outcome.summary, "h.264 kbps  720p 1442 · 480p 500 · 1 failed")
    }
}
