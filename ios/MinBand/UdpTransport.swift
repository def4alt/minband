import ARKit
import Foundation
import Network

/// Network.framework UDP client. Receives acks on the same connection.
final class UdpTransport {
    private let conn: NWConnection
    init(host: String, port: UInt16, onReceive: @escaping (Data) -> Void) {
        conn = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port)!, using: .udp)
        conn.start(queue: .global(qos: .userInitiated))
        func loop() {
            conn.receiveMessage { data, _, _, _ in if let data { onReceive(data) }; loop() }
        }
        loop()
    }
    func send(_ d: Data) { conn.send(content: d, completion: .idempotent) }
    func close() { conn.cancel() }
}
