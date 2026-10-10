package dev.minband.android

import android.content.Context
import java.io.File
import java.io.FileOutputStream
import java.util.Locale

/**
 * Per-tick CSV of all tracks for offline evaluation (tools/eval). Port of
 * ios/MinBand/GroundTruthLog.swift: header `tick,id,class,x,y,z,vx,vy,vz,conf`, one row per track
 * per logged frame, tick = edge clock (1/120 s since session start), marker frame. Rows are
 * buffered and written at most once per second (and on close).
 *
 * Files live in the app's external files directory (Android/data/dev.minband.android/files),
 * readable with `adb pull` or a USB file browser.
 */
class GroundTruthLog(directory: File, name: String? = null) {
    val file: File = File(directory, name ?: "gt-${System.currentTimeMillis() / 1000}.csv")
    private val out: FileOutputStream
    private val buffer = StringBuilder()
    private var lastFlush = System.currentTimeMillis()
    private val lock = Any()

    init {
        directory.mkdirs()
        out = FileOutputStream(file, false)
        out.write(HEADER.toByteArray())
    }

    fun append(tick: Long, tracks: List<Track>, nowMs: Long = System.currentTimeMillis()) {
        if (tracks.isEmpty()) return
        val pending: String? = synchronized(lock) {
            for (t in tracks) buffer.append(row(tick, t))
            if (nowMs - lastFlush < FLUSH_INTERVAL_MS) return@synchronized null
            lastFlush = nowMs
            val s = buffer.toString(); buffer.setLength(0); s
        }
        if (pending != null) write(pending)
    }

    /** Writes everything buffered so far. */
    fun flush() {
        val pending = synchronized(lock) { lastFlush = System.currentTimeMillis(); val s = buffer.toString(); buffer.setLength(0); s }
        if (pending.isNotEmpty()) write(pending)
        try { out.flush() } catch (_: Exception) {}
    }

    fun close() {
        flush()
        try { out.fd.sync(); out.close() } catch (_: Exception) {}
    }

    private fun write(s: String) {
        try { out.write(s.toByteArray()) } catch (_: Exception) {}
    }

    companion object {
        const val HEADER = "tick,id,class,x,y,z,vx,vy,vz,conf\n"
        const val FLUSH_INTERVAL_MS = 1000L

        fun row(tick: Long, t: Track): String {
            fun f(v: Float) = String.format(Locale.US, "%.5f", v)
            return "$tick,${t.id},${t.classId},${f(t.pos.x)},${f(t.pos.y)},${f(t.pos.z)},${f(t.vel.x)},${f(t.vel.y)},${f(t.vel.z)},${t.conf}\n"
        }

        fun directory(context: Context): File = context.getExternalFilesDir(null) ?: context.filesDir

        /** Most recent `gt-*.csv`. */
        fun latest(directory: File): File? =
            directory.listFiles { f -> f.name.startsWith("gt-") && f.name.endsWith(".csv") }?.maxByOrNull { it.lastModified() }
    }
}

/** Stable per-install device id (1..65535) for the Hello, like the iOS `DeviceIdentity`. */
object DeviceIdentity {
    fun id(context: Context): Int {
        val prefs = context.getSharedPreferences("minband", Context.MODE_PRIVATE)
        val v = prefs.getInt("deviceId", 0)
        if (v != 0) return v
        val n = 1 + java.util.Random().nextInt(0xFFFF)
        prefs.edit().putInt("deviceId", n).apply()
        return n
    }
}
