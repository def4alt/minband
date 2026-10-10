package dev.minband.android

import java.io.File
import java.io.FileOutputStream
import java.util.Locale

/**
 * Per-detection CSV next to the ground-truth log, so a phone session can be replayed through the
 * tracker on the JVM (`DetLogReplayTest`) and a tracking fault reproduced at the desk: one row per
 * lifted detection, `det-<unix>.csv` in the app's external files directory.
 *
 * `tick,i,cls,conf,x,y,z,sigma,eyex,eyey,eyez,id,bx,by,bw,bh,hpx`: tick = edge clock of the frame
 * (1/120 s since session start), `i` the box index in that frame, the lifted point and its sigma
 * (marker frame, m), the camera position at that frame, the confirmed track id the hit went to
 * (empty for a tentative or new track), the box in captured-image coordinates (0..1) and its
 * height along gravity in pixels (`WorldPoint.heightPx`; logs without the column are read as a
 * portrait phone with a 640 px wide image). Rows are buffered and written at most once per
 * second (and on close), like [GroundTruthLog].
 */
class DetectionLog(directory: File, name: String? = null) {
    val file: File = File(directory, name ?: "det-${System.currentTimeMillis() / 1000}.csv")
    private val out: FileOutputStream
    private val buffer = StringBuilder()
    private var lastFlush = System.currentTimeMillis()
    private val lock = Any()

    init {
        directory.mkdirs()
        out = FileOutputStream(file, false)
        out.write(HEADER.toByteArray())
    }

    /** `points` and `ids` are `Lift3D.lift`'s output and what `Tracker.update` returned for it. */
    fun append(tick: Long, dets: List<Detection>, points: List<WorldPoint>, ids: List<Int?>, nowMs: Long = System.currentTimeMillis()) {
        if (points.isEmpty()) return
        val pending: String? = synchronized(lock) {
            for ((p, id) in points.zip(ids)) buffer.append(row(tick, p, id, dets.getOrNull(p.detectionIndex)))
            if (nowMs - lastFlush < FLUSH_INTERVAL_MS) return@synchronized null
            lastFlush = nowMs
            val s = buffer.toString(); buffer.setLength(0); s
        }
        if (pending != null) write(pending)
    }

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
        const val HEADER = "tick,i,cls,conf,x,y,z,sigma,eyex,eyey,eyez,id,bx,by,bw,bh,hpx\n"
        const val FLUSH_INTERVAL_MS = 1000L
        /** Logs from before the `hpx` column: the phone was portrait (gravity along the captured image's x) with a 640 x 480 CPU image. */
        const val LEGACY_IMAGE_WIDTH_PX = 640f

        fun row(tick: Long, p: WorldPoint, id: Int?, det: Detection?): String {
            fun f(v: Float) = String.format(Locale.US, "%.4f", v)
            val e = p.eye
            val b = det?.bbox
            return "$tick,${p.detectionIndex},${p.classId},${f(p.conf)},${f(p.pos.x)},${f(p.pos.y)},${f(p.pos.z)},${f(p.sigma)}," +
                "${e?.let { f(it.x) } ?: ""},${e?.let { f(it.y) } ?: ""},${e?.let { f(it.z) } ?: ""},${id ?: ""}," +
                "${b?.let { f(it.x) } ?: ""},${b?.let { f(it.y) } ?: ""},${b?.let { f(it.width) } ?: ""},${b?.let { f(it.height) } ?: ""},${f(p.heightPx)}\n"
        }

        /** One row back into a lifted point (and the tick and id it carried); null for a malformed line. */
        fun parse(line: String): Parsed? {
            val c = line.split(',')
            if (c.size < 12) return null
            return try {
                val eye = if (c[8].isNotEmpty()) Vec3(c[8].toFloat(), c[9].toFloat(), c[10].toFloat()) else null
                val box = if (c.size >= 16 && c[12].isNotEmpty()) RectF(c[12].toFloat(), c[13].toFloat(), c[14].toFloat(), c[15].toFloat()) else null
                val heightPx = if (c.size >= 17 && c[16].isNotEmpty()) c[16].toFloat() else (box?.width ?: 0f) * LEGACY_IMAGE_WIDTH_PX
                Parsed(
                    c[0].toLong(),
                    WorldPoint(c[2].toInt(), Vec3(c[4].toFloat(), c[5].toFloat(), c[6].toFloat()), c[3].toFloat(), c[1].toInt(), c[7].toFloat(), eye, heightPx = heightPx),
                    c[11].toIntOrNull(), box,
                )
            } catch (_: NumberFormatException) { null }
        }

        class Parsed(val tick: Long, val point: WorldPoint, val id: Int?, val box: RectF?)
    }
}
