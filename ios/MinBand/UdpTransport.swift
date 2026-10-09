import Foundation
import Network

/// Network.framework UDP client. Receives acks on the same connection.
final class UdpTransport {
    private let conn: NWConnection
    private let queue = DispatchQueue(label: "minband.udp", qos: .userInitiated)
    private let lock = NSLock()
    private var _sentBytes = 0

    /// `host` may be "1.2.3.4" or "laptop.local"; `port` defaults to the server's 7777.
    init(host: String, port: UInt16 = 7777, onReceive: @escaping (Data) -> Void) {
        conn = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port) ?? 7777, using: .udp)
        conn.start(queue: queue)
        receive(onReceive)
    }

    /// Splits "host" or "host:port" (IPv6 literals in brackets: "[::1]:7777").
    static func parse(_ s: String, defaultPort: UInt16 = 7777) -> (host: String, port: UInt16) {
        let t = s.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("["), let close = t.firstIndex(of: "]") {
            let host = String(t[t.index(after: t.startIndex)..<close])
            let rest = t[t.index(after: close)...]
            return (host, rest.hasPrefix(":") ? UInt16(rest.dropFirst()) ?? defaultPort : defaultPort)
        }
        let parts = t.split(separator: ":")
        if parts.count == 2, let p = UInt16(parts[1]) { return (String(parts[0]), p) }
        return (t, defaultPort)
    }

    /// Payload bytes handed to the socket so far.
    var sentBytes: Int { lock.withLock { _sentBytes } }

    func send(_ d: Data) {
        guard !d.isEmpty else { return }
        lock.withLock { _sentBytes += d.count }
        conn.send(content: d, completion: .idempotent)
    }

    func close() { conn.cancel() }

    private func receive(_ onReceive: @escaping (Data) -> Void) {
        conn.receiveMessage { [weak self] data, _, _, error in
            if let data, !data.isEmpty { onReceive(data) }
            guard let self else { return }
            if case .cancelled = self.conn.state { return }
            if error != nil {
                // Typically ICMP port unreachable while the server is down; retry gently.
                self.queue.asyncAfter(deadline: .now() + 0.5) { self.receive(onReceive) }
            } else {
                self.receive(onReceive)
            }
        }
    }
}
