package dev.minband.android

import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.util.TreeMap

/**
 * Replays the phone's own track output (`gt-<unix>.csv`, 30 Hz rows per track) through
 * `OverlayMotion`, with hits, sigma and camera position taken from the matching `det-<unix>.csv`:
 * what the phone's classifier saw, as opposed to `DetLogReplayTest`, which re-runs the tracker.
 * Prints when each id became MOVING. Runs only with MINBAND_GT_LOG and MINBAND_DET_LOG set.
 */
class GtMotionReplayTest {
    @Test fun replayPhoneTracksThroughTheClassifier() {
        val gtPath = System.getenv("MINBAND_GT_LOG"); val detPath = System.getenv("MINBAND_DET_LOG")
        assumeTrue("MINBAND_GT_LOG / MINBAND_DET_LOG not set", !gtPath.isNullOrBlank() && !detPath.isNullOrBlank())
        // det: per tick, the hits (id -> sigma) and the eye.
        class Hit(val tick: Long, val id: Int, val sigma: Float)
        val hits = ArrayList<Hit>()
        val eyes = TreeMap<Long, Vec3>()
        for (line in File(detPath!!).readLines().drop(1)) {
            val p = DetectionLog.parse(line) ?: continue
            p.point.eye?.let { eyes[p.tick] = it }
            if (p.id != null) hits.add(Hit(p.tick, p.id, p.point.sigma))
        }
        // gt: per tick, the tracks.
        val frames = TreeMap<Long, MutableList<Track>>()
        for (line in File(gtPath!!).readLines().drop(1)) {
            val c = line.split(',')
            if (c.size < 10) continue
            frames.getOrPut(c[0].toLong()) { ArrayList() }.add(Track(c[1].toInt(), c[2].toInt(), Vec3(c[3].toFloat(), c[4].toFloat(), c[5].toFloat()), Vec3(c[6].toFloat(), c[7].toFloat(), c[8].toFloat()), c[9].toInt()))
        }
        val motion = OverlayMotion()
        System.getenv("MINBAND_GT_TRACE_ID")?.toIntOrNull()?.let { motion.debugId = it }
        val movingAt = LinkedHashMap<Int, Double>()
        val first = HashMap<Int, Double>()
        val t0 = frames.firstKey() / Pipeline.TICKS_PER_SECOND
        for ((tick, tracks) in frames) {
            val t = tick / Pipeline.TICKS_PER_SECOND
            val withHits = tracks.map { tr ->
                val mine = hits.filter { it.id == tr.id && it.tick <= tick }
                val sig = mine.takeLast(5).map { it.sigma }.sorted().let { if (it.isEmpty()) 0f else it[it.size / 2] }
                first.putIfAbsent(tr.id, t)
                tr.copy(hits = mine.size, sigma = sig)
            }
            val eye = eyes.floorEntry(tick)?.value
            val moving = motion.update(withHits, t, eye)
            for (id in moving) movingAt.putIfAbsent(id, t)
        }
        if (motion.debugId != null) {
            val flip = motion.debugLines.firstOrNull { it.endsWith("MOVING") }?.let { it.substring(2, it.indexOf(' ')).toDouble() }
            var last = -1.0
            for (l in motion.debugLines) {
                val tt = l.substring(2, l.indexOf(' ')).toDouble()
                if ((flip != null && kotlin.math.abs(tt - flip) <= 1.0) || tt - last >= 0.45) { println("  trace " + l); last = tt }
            }
        }
        println("phone tracks: ${first.size} ids; MOVING: " + (if (movingAt.isEmpty()) "none" else movingAt.entries.joinToString { (id, t) -> "id $id at %.1f s (born %.1f)".format(t - t0, first.getValue(id) - t0) }))
        for ((id, tf) in first) {
            val rows = frames.values.flatten().filter { it.id == id }
            val h = hits.count { it.id == id }
            println("  id %d cls %d born %.1f life %.1f s hits %d maxspeed %.2f".format(id, rows.first().classId, tf - t0, rows.size / 30.0, h, rows.maxOf { it.vel.length }))
        }
    }
}
