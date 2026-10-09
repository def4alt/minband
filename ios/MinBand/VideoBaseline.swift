import AVFoundation
import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import VideoToolbox

/// Baseline A of `tools/eval` (README "Baselines"): what the same recorded session costs as H.264
/// video, measured on the phone instead of quoted. Opt-in (the `H.264` toggle, off by default):
/// three hardware encoders cost battery and thermal headroom.
///
/// During a run `Pipeline` offers every `ARFrame.capturedImage`. At ~30 Hz, one frame at a time,
/// the image is scaled (aspect fill, `VTPixelTransferSession`) into a buffer from each adaptor's
/// pool at 1280x720, 854x480 and 640x360 and appended to three `AVAssetWriter`s: H.264 (hardware
/// VideoToolbox encoder), average bit rate 1.5 Mbps / 500 kbps / 250 kbps, 30 fps expected,
/// keyframe at least every 60 frames, no frame reordering (a live downlink cannot use B-frames),
/// real-time input, timestamps from `ARFrame.timestamp`. A writer whose input is not ready drops
/// the frame. At stop the files are finished and read back with `AVAssetReader` (no output
/// settings, so samples stay compressed); measured bps = encoded sample bytes * 8 / duration (file
/// size if the read-back fails) goes to `baseline_a-<unix>.json` next to `gt-<unix>.csv`, in the
/// format `loadBaselineA` in tools/eval/src/baselines.ts reads.
///
/// Threads: `offer` on the ARKit delegate queue (cheap, never blocks: at most one frame in flight,
/// later ones are dropped); scaling, encoding, finishing and measuring on `queue`. Only the
/// captured pixel buffer is held, and only until it has been scaled; never the ARFrame.
final class VideoBaseline {
    struct Rendition: Equatable {
        let name: String        // "720p"
        let width: Int
        let height: Int
        let targetBps: Int
        var id: String { "h264_\(name)" }            // tools/eval BASELINE_A_CONFIGURED ids
        var label: String { "H.264 \(name)" }
        var resolution: String { "\(width)x\(height)" }
    }

    static let renditions = [
        Rendition(name: "720p", width: 1280, height: 720, targetBps: 1_500_000),
        Rendition(name: "480p", width: 854, height: 480, targetBps: 500_000),
        Rendition(name: "360p", width: 640, height: 360, targetBps: 250_000),
    ]
    static let frameRate = 30
    static let frameInterval = 1.0 / Double(frameRate)
    static let maxKeyFrameInterval = 60

    /// One finished rendition. `bytes`: encoded video sample bytes, or the whole file when the
    /// read-back failed (`fromFileSize`). `seconds`: see `duration(first:last:)`. `dropped`: frames
    /// offered at 30 Hz that this writer did not get (input not ready, or still busy).
    struct Measurement: Equatable {
        let rendition: Rendition
        let bytes: Int
        let seconds: Double
        var frames = 0
        var dropped = 0
        var fromFileSize = false
    }

    /// One entry of `runs/baseline_a.json`.
    struct Entry: Codable, Equatable {
        let id: String
        let bps: Int
        let label: String
        let resolution: String
        let source: String
    }

    struct Report: Codable, Equatable { let entries: [Entry] }

    /// What `finish` produced: the entries written to `jsonURL`, and what went wrong per rendition.
    struct Outcome {
        var entries: [Entry] = []
        var problems: [String] = []
        var jsonURL: URL?

        /// One line for the HUD notices.
        var summary: String {
            if entries.isEmpty { return "h.264 baseline failed: \(problems.first ?? "no frames")" }
            let s = VideoBaseline.summary(entries)
            return problems.isEmpty ? s : "\(s) · \(problems.count) failed"
        }
    }

    struct Failure: Error, LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }

    // MARK: pure (unit-tested in MinBandTests/VideoBaselineTests)

    /// Average bit rate, rounded; nil without bytes or time (tools/eval rejects bps <= 0).
    static func bitsPerSecond(bytes: Int, seconds: Double) -> Int? {
        guard bytes > 0, seconds > 0, seconds.isFinite else { return nil }
        let bps = (Double(bytes) * 8 / seconds).rounded()
        return bps >= 1 ? Int(bps) : nil
    }

    /// Time covered by frames presented from `first` to `last` (seconds): the last frame lasts one
    /// frame interval too.
    static func duration(first: Double, last: Double) -> Double { last - first + frameInterval }

    /// "1.5 Mbps", "500 kbps".
    static func rateText(_ bps: Int) -> String {
        bps >= 1_000_000 ? String(format: "%g Mbps", Double(bps) / 1_000_000) : String(format: "%g kbps", Double(bps) / 1000)
    }

    /// The JSON entry for one rendition, nil when nothing was encoded.
    static func entry(_ m: Measurement, session: String) -> Entry? {
        guard let bps = bitsPerSecond(bytes: m.bytes, seconds: m.seconds) else { return nil }
        let r = m.rendition
        var source = "measured: AVAssetWriter H.264 \(r.name)\(frameRate) target \(rateText(r.targetBps)), "
            + "session \(session), \(String(format: "%.1f", m.seconds)) s"
        if m.dropped > 0 { source += ", \(m.dropped) of \(m.frames + m.dropped) frames dropped" }
        if m.fromFileSize { source += ", file size incl. container" }
        return Entry(id: r.id, bps: bps, label: r.label, resolution: r.resolution, source: source)
    }

    /// `{ "entries": [ ... ] }`, as `loadBaselineA` reads it.
    static func reportJSON(_ entries: [Entry]) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return try encoder.encode(Report(entries: entries))
    }

    /// "h.264 kbps  720p 1442 · 480p 495 · 360p 251".
    static func summary(_ entries: [Entry]) -> String {
        let parts = entries.map { e -> String in
            let name = renditions.first { $0.id == e.id }?.name ?? e.id
            return "\(name) \(Int((Double(e.bps) / 1000).rounded()))"
        }
        return "h.264 kbps  " + parts.joined(separator: " · ")
    }

    /// "gt-1712345.csv" -> "1712345" (any other name: its stem).
    static func sessionId(forLog log: URL) -> String {
        let stem = log.deletingPathExtension().lastPathComponent
        return stem.hasPrefix("gt-") ? String(stem.dropFirst(3)) : stem
    }

    /// `baseline_a-<unix>.json` next to the ground-truth log `gt-<unix>.csv`.
    static func reportURL(forLog log: URL) -> URL {
        log.deletingLastPathComponent().appendingPathComponent("baseline_a-\(sessionId(forLog: log)).json")
    }

    /// `h264_720p-<unix>.mp4` next to the ground-truth log.
    static func videoURL(forLog log: URL, rendition r: Rendition) -> URL {
        log.deletingLastPathComponent().appendingPathComponent("\(r.id)-\(sessionId(forLog: log)).mp4")
    }

    // MARK: recording

    /// Session name in the JSON `source`, the log's stem ("gt-1712345").
    let session: String
    let reportURL: URL
    private let encoders: [Encoder]          // queue only after init
    private let queue = DispatchQueue(label: "minband.h264", qos: .userInitiated)
    // arQueue only (the caller of `offer`)
    private var firstTime: TimeInterval?
    private var lastOffer: TimeInterval = -Double.infinity
    // lock
    private let lock = NSLock()
    private var busy = false                 // a frame is being scaled and appended
    private var skipped = 0                  // 30 Hz frames dropped because the previous was busy

    /// Sets up the three writers next to the ground-truth log `log` (`gt-<unix>.csv`). Nothing is
    /// written until the first frame.
    init(log: URL) throws {
        session = log.deletingPathExtension().lastPathComponent
        reportURL = VideoBaseline.reportURL(forLog: log)
        encoders = try VideoBaseline.renditions.map {
            try Encoder($0, url: VideoBaseline.videoURL(forLog: log, rendition: $0))
        }
    }

    /// ARKit delegate queue, every frame. Keeps ~30 Hz and hands the image to the encoder queue;
    /// returns at once. A frame that arrives while the previous one is still being scaled is
    /// dropped (counted), so neither ARKit's buffers nor this queue ever pile up.
    func offer(_ image: CVPixelBuffer, time: TimeInterval) {
        guard time - lastOffer >= VideoBaseline.frameInterval - 0.004 else { return }
        lastOffer = time
        let start = firstTime ?? time
        firstTime = start
        let accepted: Bool = lock.withLock {
            if busy { skipped += 1; return false }
            busy = true
            return true
        }
        guard accepted else { return }
        let seconds = time - start
        queue.async {
            let pts = CMTime(seconds: seconds, preferredTimescale: 90_000)
            for e in self.encoders { e.append(image, pts: pts, seconds: seconds) }
            self.lock.withLock { self.busy = false }
        }
    }

    /// Stops recording: finishes the files, measures them and writes `reportURL`. Call once, after
    /// the last `offer`. `completion` runs on the encoder queue.
    func finish(_ completion: @escaping (Outcome) -> Void) {
        queue.async {
            let group = DispatchGroup()
            for e in self.encoders where e.writer.status == .writing {
                e.input.markAsFinished()
                group.enter()
                e.writer.finishWriting { group.leave() }
            }
            group.notify(queue: self.queue) { self.measure(completion) }
        }
    }

    // MARK: internals (queue)

    private func measure(_ completion: @escaping (Outcome) -> Void) {
        let group = DispatchGroup()
        for e in encoders where e.writer.status == .completed {
            group.enter()
            let asset = AVURLAsset(url: e.url)
            asset.loadTracks(withMediaType: .video) { tracks, _ in
                self.queue.async {
                    if let track = tracks?.first {
                        e.encodedBytes = VideoBaseline.sampleBytes(asset: asset, track: track)
                    }
                    group.leave()
                }
            }
        }
        group.notify(queue: queue) { completion(self.report()) }
    }

    /// Sum of the compressed sample sizes of `track`. No output settings: nothing is decoded.
    private static func sampleBytes(asset: AVAsset, track: AVAssetTrack) -> Int? {
        guard let reader = try? AVAssetReader(asset: asset) else { return nil }
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: nil)
        output.alwaysCopiesSampleData = false
        guard reader.canAdd(output) else { return nil }
        reader.add(output)
        guard reader.startReading() else { return nil }
        var total = 0
        while let sample = output.copyNextSampleBuffer() {
            total += CMSampleBufferGetTotalSampleSize(sample)
        }
        return reader.status == .completed && total > 0 ? total : nil
    }

    private func report() -> Outcome {
        let busyDrops = lock.withLock { skipped }
        var out = Outcome()
        var measured: [Measurement] = []
        for e in encoders {
            let name = e.rendition.name
            guard e.writer.status == .completed, let first = e.firstSeconds else {
                let why = e.failure ?? e.writer.error?.localizedDescription ?? "no frames"
                out.problems.append("\(name): \(why)")
                continue
            }
            var bytes = e.encodedBytes ?? 0
            var fromFileSize = false
            if bytes <= 0, let size = try? e.url.resourceValues(forKeys: [.fileSizeKey]).fileSize {
                bytes = size
                fromFileSize = true
            }
            measured.append(Measurement(rendition: e.rendition, bytes: bytes,
                                        seconds: VideoBaseline.duration(first: first, last: e.lastSeconds),
                                        frames: e.frames, dropped: e.dropped + busyDrops,
                                        fromFileSize: fromFileSize))
        }
        for m in measured {
            if let entry = VideoBaseline.entry(m, session: session) {
                out.entries.append(entry)
            } else {
                out.problems.append("\(m.rendition.name): nothing encoded")
            }
        }
        if !out.entries.isEmpty {
            do {
                try VideoBaseline.reportJSON(out.entries).write(to: reportURL, options: .atomic)
                out.jsonURL = reportURL
            } catch {
                out.problems.append("json: \(error.localizedDescription)")
            }
        }
        for p in out.problems { NSLog("MinBand VideoBaseline: %@", p) }
        for e in out.entries { NSLog("MinBand VideoBaseline: %@", "\(e.id) \(e.bps) bps, \(e.source)") }
        return out
    }

    /// One writer, its input and pixel buffer adaptor, and a scaler into the adaptor's pool.
    /// Used on the encoder queue only.
    private final class Encoder {
        let rendition: Rendition
        let url: URL
        let writer: AVAssetWriter
        let input: AVAssetWriterInput
        let adaptor: AVAssetWriterInputPixelBufferAdaptor
        let scaler: VTPixelTransferSession
        var started = false
        var failure: String?
        var firstSeconds: Double?      // first appended frame, seconds since the first offer
        var lastSeconds: Double = 0
        var frames = 0
        var dropped = 0
        var encodedBytes: Int?         // set by `measure`

        init(_ r: Rendition, url: URL) throws {
            try? FileManager.default.removeItem(at: url)   // the writer fails if the file exists
            let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
            let compression: [String: Any] = [
                AVVideoAverageBitRateKey: r.targetBps,
                AVVideoExpectedSourceFrameRateKey: VideoBaseline.frameRate,
                AVVideoMaxKeyFrameIntervalKey: VideoBaseline.maxKeyFrameInterval,
                AVVideoAllowFrameReorderingKey: false,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
            ]
            let settings: [String: Any] = [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: r.width,
                AVVideoHeightKey: r.height,
                AVVideoCompressionPropertiesKey: compression,
            ]
            let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
            input.expectsMediaDataInRealTime = true
            // The sensor image is landscape; the app is portrait. Display metadata only.
            input.transform = CGAffineTransform(rotationAngle: .pi / 2)
            // Same format as ARFrame.capturedImage (full-range 4:2:0 bi-planar), so the scaler only scales.
            let attributes: [String: Any] = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange,
                kCVPixelBufferWidthKey as String: r.width,
                kCVPixelBufferHeightKey as String: r.height,
                kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
            ]
            let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: attributes)
            guard writer.canAdd(input) else { throw Failure("\(r.name): cannot add the video input") }
            writer.add(input)

            var created: VTPixelTransferSession?
            let status = VTPixelTransferSessionCreate(allocator: kCFAllocatorDefault, pixelTransferSessionOut: &created)
            guard status == 0, let scaler = created else { throw Failure("\(r.name): no pixel transfer session (\(status))") }
            // Aspect fill: the 4:3 sensor image is cropped to 16:9, as a 16:9 camera would see it.
            _ = VTSessionSetProperty(scaler, key: kVTPixelTransferPropertyKey_ScalingMode, value: kVTScalingMode_Trim)

            self.rendition = r
            self.url = url
            self.writer = writer
            self.input = input
            self.adaptor = adaptor
            self.scaler = scaler
        }

        deinit { VTPixelTransferSessionInvalidate(scaler) }

        func append(_ image: CVPixelBuffer, pts: CMTime, seconds: Double) {
            guard failure == nil else { return }
            if !started {
                started = true
                guard writer.startWriting() else {
                    failure = "start: \(writer.error?.localizedDescription ?? "unknown error")"
                    return
                }
                writer.startSession(atSourceTime: pts)
            }
            guard writer.status == .writing else {
                failure = writer.error?.localizedDescription ?? "writer stopped"
                return
            }
            // The pool exists once the session has started; nil again if the writer failed.
            guard input.isReadyForMoreMediaData, let pool = adaptor.pixelBufferPool else { dropped += 1; return }
            var created: CVPixelBuffer?
            guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &created) == kCVReturnSuccess,
                  let buffer = created,
                  VTPixelTransferSessionTransferImage(scaler, from: image, to: buffer) == 0 else {
                dropped += 1
                return
            }
            guard adaptor.append(buffer, withPresentationTime: pts) else {
                failure = "append: \(writer.error?.localizedDescription ?? "unknown error")"
                return
            }
            if firstSeconds == nil { firstSeconds = seconds }
            lastSeconds = seconds
            frames += 1
        }
    }
}
