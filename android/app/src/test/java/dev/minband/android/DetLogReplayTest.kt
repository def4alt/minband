package dev.minband.android

import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.util.TreeMap
import kotlin.math.hypot

/**
 * Replays a phone session's detection log (`det-<unix>.csv`, [DetectionLog]) through `Tracker`
 * and `OverlayMotion` and prints what the tracker makes of those very hits: one line per id with
 * class, lifetime, net displacement, top speed and whether it was ever MOVING, then totals. The
 * same hits at the desk, in a second, after any tracker change. Runs only when `MINBAND_DET_LOG`
 * names the file:
 *
 * ```
 * adb pull /sdcard/Android/data/dev.minband.android/files/det-<unix>.csv .
 * MINBAND_DET_LOG=$PWD/det-<unix>.csv ./gradlew :app:testDebugUnitTest --tests "*DetLogReplay*" -i
 * ```
 * (PowerShell: `$env:MINBAND_DET_LOG = "D:\...\det-<unix>.csv"; .\gradlew ...`.) The output is also
 * in `app/build/test-results/testDebugUnitTest/TEST-dev.minband.android.DetLogReplayTest.xml`.
 */
class DetLogReplayTest {
    private class Life(val cls: Int, val first: Double, var last: Double, val start: Vec3, var pos: Vec3, var maxSpeed: Float = 0f, var moving: Boolean = false, var movingAt: Double = -1.0)

    @Test fun replayPhoneDetectionLog() {
        val path = System.getenv("MINBAND_DET_LOG")
        assumeTrue("MINBAND_DET_LOG not set: nothing to replay", !path.isNullOrBlank())
        val frames = TreeMap<Long, MutableList<WorldPoint>>()
        var phoneIds = HashSet<Int>()
        for (line in File(path!!).readLines().drop(1)) {
            val p = DetectionLog.parse(line) ?: continue
            frames.getOrPut(p.tick) { ArrayList() }.add(p.point)
            p.id?.let { phoneIds.add(it) }
        }
        assumeTrue("no rows in $path", frames.isNotEmpty())

        val tracker = Tracker(Tracker.Config.SLOW_DETECTOR)
        val motion = OverlayMotion()
        // MINBAND_DET_DEBUG_ID=<replay id>: the classifier's judgement of that track, line by line.
        System.getenv("MINBAND_DET_DEBUG_ID")?.toIntOrNull()?.let { motion.debugId = it }
        val trace = System.getenv("MINBAND_DET_TRACE")?.split(',')?.takeIf { it.size == 2 }?.let { it[0].toDouble() to it[1].toDouble() }
        val lives = LinkedHashMap<Int, Life>()
        val ticks = frames.keys.iterator()
        var next: Long? = ticks.next()
        val tickDt = 1.0 / 30
        var t = frames.firstKey() / Pipeline.TICKS_PER_SECOND
        val end = frames.lastKey() / Pipeline.TICKS_PER_SECOND + 2.5
        var eye: Vec3? = null
        while (t <= end) {
            while (next != null && next / Pipeline.TICKS_PER_SECOND <= t) {
                val pts = frames.getValue(next)
                val ids = tracker.update(pts, next / Pipeline.TICKS_PER_SECOND)
                pts.firstOrNull { it.eye != null }?.let { eye = it.eye }
                // MINBAND_DET_TRACE="from,to" (s since the first frame): print every hit of those
                // frames with the id the replayed tracker gave it, to see an association go wrong.
                trace?.let { (a, b) ->
                    val ft = next / Pipeline.TICKS_PER_SECOND - frames.firstKey() / Pipeline.TICKS_PER_SECOND
                    if (ft in a..b) for ((p, id) in pts.zip(ids)) {
                        val e = p.eye
                        val bearing = if (e != null) Math.toDegrees(Math.atan2((p.pos.x - e.x).toDouble(), -(p.pos.z - e.z).toDouble())) else Double.NaN
                        val range = if (e != null) Math.hypot((p.pos.x - e.x).toDouble(), (p.pos.z - e.z).toDouble()) else Double.NaN
                        println("  t=%5.1f cls %2d bearing %6.1f range %4.1f sigma %.2f -> %s".format(ft, p.classId, bearing, range, p.sigma, id?.toString() ?: "-"))
                    }
                }
                next = if (ticks.hasNext()) ticks.next() else null
            }
            val tracks = tracker.tracks(t)
            val moving = motion.update(tracks, t, eye)
            for (tr in tracks) {
                val l = lives.getOrPut(tr.id) { Life(tr.classId, t, t, tr.pos, tr.pos) }
                l.last = t; l.pos = tr.pos; l.maxSpeed = maxOf(l.maxSpeed, tr.vel.length)
                if (tr.id in moving) { if (!l.moving) l.movingAt = t; l.moving = true }
            }
            t += tickDt
        }

        val span = (frames.lastKey() - frames.firstKey()) / Pipeline.TICKS_PER_SECOND
        println("${frames.size} detection frames over %.1f s (%.2f Hz), %d hits; the phone confirmed %d ids".format(span, frames.size / span, frames.values.sumOf { it.size }, phoneIds.size))
        val t0 = frames.firstKey() / Pipeline.TICKS_PER_SECOND
        println(" id cls   from     to  life_s  net_m  maxspeed  moving")
        for ((id, l) in lives) {
            println("%3d %3d %6.1f %6.1f %7.1f %6.2f %9.2f  %s".format(id, l.cls, l.first - t0, l.last - t0, l.last - l.first, hypot((l.pos.x - l.start.x).toDouble(), (l.pos.z - l.start.z).toDouble()), l.maxSpeed, if (l.moving) "MOVING at %.1f".format(l.movingAt - t0) else ""))
        }
        motion.debugLines.forEach { println("  trace $it") }
        val lifeList = lives.values.map { it.last - it.first }.sorted()
        println("replay: %d ids, median life %.1f s, %d ever MOVING".format(lives.size, if (lifeList.isEmpty()) 0.0 else lifeList[lifeList.size / 2], lives.values.count { it.moving }))
        val byClass = lives.values.groupBy { it.cls }.toSortedMap()
        println("by class (ids / MOVING / max speed): " + byClass.entries.joinToString("  ") { (c, l) -> "%d: %d/%d/%.1f".format(c, l.size, l.count { it.moving }, l.maxOf { it.maxSpeed }) })
    }
}
