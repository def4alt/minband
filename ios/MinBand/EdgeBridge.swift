import ARKit
import Foundation
import Network

/// Thin wrapper over the uniffi-generated bindings (ios/MinBand/Generated). Until M1 wires
/// uniffi, this compiles with the empty stubs so the app can run with the AR preview.
final class EdgeBridge {
    struct Stats { var seq: UInt32 = 0; var thetaScale: Double = 1; var bytesPerSecEstimate: Int = 0 }
    init(deviceId: UInt32, sessionNonce: UInt32) {}
    func tick(tracks: [Track], now: UInt32) -> [Data] { [] }   // TODO(M1): call core
    func onDatagram(_ d: Data) {}                               // TODO(M1)
    func stats() -> Stats { Stats() }                           // TODO(M1)
    func pose(_ t: simd_float4x4, tick: UInt32) -> Data { Data() } // TODO(M1)
}
