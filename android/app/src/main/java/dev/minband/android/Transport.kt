package dev.minband.android

import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.PortUnreachableException
import java.net.SocketException
import java.net.SocketTimeoutException
import java.util.concurrent.atomic.AtomicLong

/**
 * Where datagrams go. [UdpTransport] talks to the server directly over Wi-Fi (as the iOS app
 * does); a serial/BLE bridge to a NUCODE board implements the same two calls.
 */
interface Transport {
    /** Payload bytes handed to the link so far. */
    val sentBytes: Long
    fun send(d: ByteArray)
    fun close()
}

/** UDP client to the MinBand server (default port 7777). Receives acks on the same socket. */
class UdpTransport(host: String, port: Int = DEFAULT_PORT, private val onReceive: (ByteArray) -> Unit) : Transport {
    private val socket = DatagramSocket()
    private val target: InetSocketAddress = InetSocketAddress(InetAddress.getByName(host), port)
    private val sent = AtomicLong()
    @Volatile private var closed = false
    private val receiver = Thread({ receiveLoop() }, "minband.udp").apply { isDaemon = true }

    init {
        socket.soTimeout = 500
        receiver.start()
    }

    override val sentBytes: Long get() = sent.get()

    override fun send(d: ByteArray) {
        if (d.isEmpty() || closed) return
        sent.addAndGet(d.size.toLong())
        try {
            socket.send(DatagramPacket(d, d.size, target))
        } catch (e: Exception) {
            Log.w(TAG, "send: ${e.message}")
        }
    }

    override fun close() {
        closed = true
        socket.close()
    }

    private fun receiveLoop() {
        val buf = ByteArray(2048)
        val packet = DatagramPacket(buf, buf.size)
        while (!closed) {
            try {
                socket.receive(packet)
                if (packet.length > 0) onReceive(packet.data.copyOf(packet.length))
            } catch (_: SocketTimeoutException) {
            } catch (_: PortUnreachableException) {
                // ICMP port unreachable while the server is down; retry gently.
                try { Thread.sleep(500) } catch (_: InterruptedException) { return }
            } catch (e: SocketException) {
                if (!closed) Log.w(TAG, "receive: ${e.message}")
                return
            } catch (e: Exception) {
                Log.w(TAG, "receive: ${e.message}")
            }
        }
    }

    companion object {
        const val DEFAULT_PORT = 7777
        private const val TAG = "MinBand UDP"

        /** Splits "host" or "host:port" (IPv6 literals in brackets: "[::1]:7777"). */
        fun parse(s: String, defaultPort: Int = DEFAULT_PORT): Pair<String, Int> {
            val t = s.trim()
            if (t.startsWith("[")) {
                val close = t.indexOf(']')
                if (close > 0) {
                    val host = t.substring(1, close)
                    val rest = t.substring(close + 1)
                    val port = if (rest.startsWith(":")) rest.substring(1).toIntOrNull() ?: defaultPort else defaultPort
                    return Pair(host, port)
                }
            }
            val parts = t.split(':')
            if (parts.size == 2) parts[1].toIntOrNull()?.let { return Pair(parts[0], it) }
            return Pair(t, defaultPort)
        }
    }
}
