import ARKit
import CoreML
import Foundation
import ImageIO
import Vision

/// Vision + CoreML YOLO on the ARFrame's captured image. 320 px input, COCO subset.
///
/// Model: the first `.mlmodelc` / `.mlpackage` / `.mlmodel` found in the app bundle (bundle root,
/// where Xcode puts models it compiled at build time, or `Models/` for models copied verbatim).
/// Uncompiled models are compiled once at runtime and cached in Application Support. The model must
/// end in a non-maximum-suppression stage so Vision returns `VNRecognizedObjectObservation`s
/// (Ultralytics: `export(format="coreml", nms=True)`, see ios/README.md). With no model,
/// `isAvailable` is false and `detect` returns [].
///
/// Not thread-safe: create and use it on one serial queue (Pipeline's detection queue).
final class Detector {
    static let confidenceThreshold: Float = 0.35
    static let iouThreshold: Double = 0.45

    private(set) var isAvailable = false
    private(set) var modelName: String?
    private(set) var lastError: String?
    private var request: VNCoreMLRequest?

    init(bundle: Bundle = .main) {
        for url in Detector.modelURLs(in: bundle) {
            do {
                try setUp(modelURL: url)
                modelName = url.deletingPathExtension().lastPathComponent
                isAvailable = true
                return
            } catch {
                lastError = "\(url.lastPathComponent): \(error.localizedDescription)"
                NSLog("MinBand Detector: failed to load %@", lastError ?? "")
            }
        }
        if lastError == nil { lastError = "no CoreML model in the app bundle (see ios/README.md)" }
    }

    /// Detect on the frame's captured image. `orientation` is the rotation that makes the sensor
    /// image upright for the user; `.right` for the portrait-locked app. Returned boxes are in
    /// normalized captured-image coordinates (sensor orientation, origin top-left).
    func detect(_ frame: ARFrame, orientation: CGImagePropertyOrientation = .right) -> [Detection] {
        detect(pixelBuffer: frame.capturedImage, orientation: orientation)
    }

    func detect(pixelBuffer: CVPixelBuffer, orientation: CGImagePropertyOrientation) -> [Detection] {
        guard let request else { return [] }
        let handler = VNImageRequestHandler(cvPixelBuffer: pixelBuffer, orientation: orientation, options: [:])
        do { try handler.perform([request]) } catch {
            lastError = error.localizedDescription
            return []
        }
        guard let results = request.results as? [VNRecognizedObjectObservation] else { return [] }
        var out: [Detection] = []
        for obs in results {
            guard let top = obs.labels.first, top.confidence >= Detector.confidenceThreshold,
                  let cls = Detector.classId(forLabel: top.identifier) else { continue }
            let rect = Detector.capturedImageRect(fromVision: obs.boundingBox, orientation: orientation)
            out.append(Detection(classId: cls, bbox: rect, conf: top.confidence))
        }
        return out
    }

    // MARK: label mapping

    /// COCO label -> tracked class id (core/src/classes.rs), nil for classes we do not track.
    /// Accepts the usual spellings across exports and bare COCO indices ("0", "67").
    static func classId(forLabel label: String) -> UInt8? {
        let l = label.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            .replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ")
        if let id = TrackedClass.byLabel[l] { return id }
        switch l {
        case "cellphone", "mobile phone", "phone": return TrackedClass.cellPhone
        case "tvmonitor", "tv monitor", "television", "monitor": return TrackedClass.tv
        case "people", "pedestrian": return TrackedClass.person
        default: break
        }
        if let n = UInt8(l), TrackedClass.all.contains(n) { return n }
        return nil
    }

    // MARK: geometry

    /// Vision bounding box (normalized, origin bottom-left, in the *oriented* image) -> normalized
    /// rect in the raw captured image (sensor orientation, origin top-left).
    static func capturedImageRect(fromVision b: CGRect, orientation: CGImagePropertyOrientation) -> CGRect {
        // Oriented image, top-left origin.
        let ox = b.minX, oy = 1 - b.maxY, ow = b.width, oh = b.height
        switch orientation {
        case .right, .rightMirrored:  // displayed = raw rotated 90 deg clockwise (portrait)
            return CGRect(x: oy, y: 1 - ox - ow, width: oh, height: ow)
        case .left, .leftMirrored:    // displayed = raw rotated 90 deg counter-clockwise
            return CGRect(x: 1 - oy - oh, y: ox, width: oh, height: ow)
        case .down, .downMirrored:    // 180 deg
            return CGRect(x: 1 - ox - ow, y: 1 - oy - oh, width: ow, height: oh)
        default:                      // .up: landscape right, no rotation
            return CGRect(x: ox, y: oy, width: ow, height: oh)
        }
    }

    // MARK: model loading

    static func modelURLs(in bundle: Bundle) -> [URL] {
        var urls: [URL] = []
        for ext in ["mlmodelc", "mlpackage", "mlmodel"] {
            for sub in ["Models", nil] as [String?] {
                urls += bundle.urls(forResourcesWithExtension: ext, subdirectory: sub) ?? []
            }
        }
        var seen = Set<String>()
        let unique = urls.filter { seen.insert($0.standardizedFileURL.path).inserted }
        // Compiled first, then prefer YOLO-named models, then by name for determinism.
        func rank(_ u: URL) -> Int {
            (u.pathExtension == "mlmodelc" ? 0 : u.pathExtension == "mlpackage" ? 2 : 4)
                + (u.lastPathComponent.lowercased().contains("yolo") ? 0 : 1)
        }
        return unique.sorted { (rank($0), $0.lastPathComponent) < (rank($1), $1.lastPathComponent) }
    }

    /// Returns a URL to a compiled model, compiling (and caching) `.mlpackage` / `.mlmodel`.
    static func compiledModelURL(for url: URL) throws -> URL {
        if url.pathExtension == "mlmodelc" { return url }
        let fm = FileManager.default
        let cacheDir = try fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("CompiledModels", isDirectory: true)
        try fm.createDirectory(at: cacheDir, withIntermediateDirectories: true)
        let stamp = (try? fm.attributesOfItem(atPath: url.path)[.modificationDate] as? Date)
            .map { Int($0.timeIntervalSince1970) } ?? 0
        let cached = cacheDir.appendingPathComponent("\(url.deletingPathExtension().lastPathComponent)-\(stamp).mlmodelc")
        if fm.fileExists(atPath: cached.path) { return cached }

        // Blocking wait on the async compile; we are on the detection queue, never main.
        var result: Result<URL, Error> = .failure(CocoaError(.fileReadUnknown))
        let done = DispatchSemaphore(value: 0)
        MLModel.compileModel(at: url) { r in result = r; done.signal() }
        done.wait()
        let tmp = try result.get()
        try? fm.removeItem(at: cached)
        try fm.moveItem(at: tmp, to: cached)
        return cached
    }

    private func setUp(modelURL: URL) throws {
        let cfg = MLModelConfiguration()
        cfg.computeUnits = .all
        let ml = try MLModel(contentsOf: try Detector.compiledModelURL(for: modelURL), configuration: cfg)
        let vn = try VNCoreMLModel(for: ml)
        // Ultralytics NMS pipelines declare iou/confidence thresholds as (non-optional) inputs.
        let inputs = ml.modelDescription.inputDescriptionsByName
        var extra: [String: MLFeatureValue] = [:]
        if inputs["iouThreshold"] != nil { extra["iouThreshold"] = MLFeatureValue(double: Detector.iouThreshold) }
        if inputs["confidenceThreshold"] != nil {
            extra["confidenceThreshold"] = MLFeatureValue(double: Double(Detector.confidenceThreshold))
        }
        if !extra.isEmpty { vn.featureProvider = ThresholdInputs(values: extra) }
        let req = VNCoreMLRequest(model: vn)
        req.imageCropAndScaleOption = .scaleFill
        request = req
    }
}

private final class ThresholdInputs: NSObject, MLFeatureProvider {
    let values: [String: MLFeatureValue]
    init(values: [String: MLFeatureValue]) { self.values = values }
    var featureNames: Set<String> { Set(values.keys) }
    func featureValue(for featureName: String) -> MLFeatureValue? { values[featureName] }
}
