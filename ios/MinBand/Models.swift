import CoreGraphics
import Foundation
import simd

// Value types shared by the perception pipeline. Kept free of ARKit/Vision so the tracker and
// its tests compile anywhere (see ios/MinBandTests).

/// One 2D detection. `bbox` is in normalized coordinates of `ARFrame.capturedImage` in its native
/// (landscape sensor) orientation, origin top-left, x right, y down. That is the space
/// `ARFrame.raycastQuery(from:)`, `ARCamera.intrinsics` (after scaling by `imageResolution`) and
/// `ARFrame.displayTransform(for:viewportSize:)` all expect.
struct Detection { let classId: UInt8; let bbox: CGRect; let conf: Float }

/// Tracker output, one per confirmed track. Marker frame, metres and m/s; `conf` 0..255.
/// This is what `EdgeBridge.tick(tracks:now:)` consumes.
struct Track { let id: UInt32; let classId: UInt8; let pos: SIMD3<Float>; let vel: SIMD3<Float>; let conf: UInt8 }

/// A lifted detection: 3D point in the marker frame (Y up). `detectionIndex` points back into the
/// `[Detection]` it came from so the overlay can label boxes with track ids.
struct WorldPoint {
    let classId: UInt8
    let pos: SIMD3<Float>
    let conf: Float
    var detectionIndex: Int = -1
}

/// A detection box ready for the SwiftUI overlay: `rect` in normalized *view* coordinates
/// (origin top-left), already mapped through `ARFrame.displayTransform`.
struct OverlayBox: Identifiable {
    let id: Int
    let rect: CGRect
    let classId: UInt8
    let conf: Float
    let trackId: UInt32?
}

/// The tracked COCO subset (indices match core/src/classes.rs) and per-class motion limits that
/// mirror the core predictor's priors.
enum TrackedClass {
    static let person: UInt8 = 0
    static let backpack: UInt8 = 24
    static let handbag: UInt8 = 26
    static let bottle: UInt8 = 39
    static let cup: UInt8 = 41
    static let chair: UInt8 = 56
    static let tv: UInt8 = 62
    static let laptop: UInt8 = 63
    static let cellPhone: UInt8 = 67

    /// COCO label (as written by Ultralytics / most CoreML YOLO exports) -> class id.
    static let byLabel: [String: UInt8] = [
        "person": person, "backpack": backpack, "handbag": handbag, "bottle": bottle, "cup": cup,
        "chair": chair, "tv": tv, "laptop": laptop, "cell phone": cellPhone,
    ]

    static let all: Set<UInt8> = Set(byLabel.values)

    static func name(_ id: UInt8) -> String {
        byLabel.first(where: { $0.value == id })?.key ?? "class \(id)"
    }

    /// Hard speed cap, same values as `ClassPrior::max_speed` in core/src/classes.rs:
    /// person and carried objects 3 m/s, placed objects (chair, laptop, tv) 1 m/s.
    static func maxSpeed(_ id: UInt8) -> Float {
        switch id {
        case chair, laptop, tv: return 1.0
        default: return 3.0
        }
    }

    /// Association gate in metres (DESIGN 3.1: per-class gating).
    static func gate(_ id: UInt8) -> Float { id == person ? 0.7 : 0.4 }
}
