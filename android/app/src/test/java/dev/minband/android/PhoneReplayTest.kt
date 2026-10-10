package dev.minband.android

import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.TreeMap
import kotlin.math.cos
import kotlin.math.sin

/**
 * The phone's tracking chain on a real pedestrian clip: the phone's own detector output
 * (`tools/movers/phone_detect.py` on `tools/footage/clips/vtest.avi`, OpenCV's sample clip: a
 * fixed camera about 5 m up looking down at a plaza, 768x576 at 10 fps, 14 walkers) through
 * `Lift3D.lift` (ray to the floor), `Tracker` and `OverlayMotion` at 30 Hz, exactly as `Pipeline`
 * wires them, at the detector rates the phone reaches: 10 Hz (near the 12 Hz design rate) down to
 * 1.25 Hz (the CPU detector when the phone is hot). The camera model is the ground fit
 * `tools/footage/track.py` made for this clip (f 419 px, pitch 42.2 deg, 5.1 m).
 *
 * What it guards: a walker must get a confirmed track and the MOVING tag at every one of those
 * rates. At 2-3 detections per second a person at 1.3 m/s moves 0.4-0.7 m between hits, and a
 * fixed 0.7 m association gate loses them (no track, so no MOVING, while standing people track
 * fine): this test reproduced that on the clip before the gate was made rate-aware.
 */
class PhoneReplayTest {
    /** Frame size and rate, and the camera the boxes are lifted with (the `tools/footage` ground fit; the vtest values are the default). */
    private class Meta(val width: Int, val height: Int, val fps: Double, val f: Float = 419f, val pitchDeg: Double = 42.2, val height_m: Float = 5.1f, val maxRange: Float = 8f)

    private class Clip(val meta: Meta, val frames: TreeMap<Int, MutableList<Detection>>)

    private class Run(val detectorHz: Double, val noise: Float, val confirmed: Set<Int>, val moving: Set<Int>, val medianMovingSpeed: Float, val medianLifeS: Double, val boxesPerFrame: Double, val walkers: Int) {
        override fun toString() =
            "detector %.2f Hz, lift noise %.2f m: %.1f boxes/frame, confirmed tracks %d (median life %.1f s), MOVING %d (%d at a walking pace), median moving speed %.2f m/s"
                .format(detectorHz, noise, boxesPerFrame, confirmed.size, medianLifeS, moving.size, walkers, medianMovingSpeed)
    }

    private fun load(resource: String): Clip? {
        val text = javaClass.getResourceAsStream(resource)?.bufferedReader()?.readText() ?: return null
        val lines = text.lineSequence().filter { it.isNotBlank() }.toList()
        val kv = lines.first().removePrefix("#").trim().split(' ').associate { val (k, v) = it.split('='); k to v }
        val meta = Meta(kv.getValue("width").toInt(), kv.getValue("height").toInt(), kv.getValue("fps").toDouble(),
            kv["cam_f"]?.toFloat() ?: 419f, kv["cam_pitch"]?.toDouble() ?: 42.2, kv["cam_height"]?.toFloat() ?: 5.1f,
            // Drone footage: people 30-120 m from the camera; the phone's 8 m cull would keep none.
            maxRange = if (kv["source"] == "footage") 150f else 8f)
        val frames = TreeMap<Int, MutableList<Detection>>()
        for (line in lines.drop(2)) {
            val c = line.split(',')
            val det = Detection(c[2].toInt(), RectF(c[4].toFloat(), c[5].toFloat(), c[6].toFloat(), c[7].toFloat()), c[3].toFloat())
            frames.getOrPut(c[0].toInt()) { ArrayList() }.add(det)
        }
        return Clip(meta, frames)
    }

    /** A level camera `height` m up, pitched `pitchDeg` down, looking along world -Z. */
    private fun camera(pitchDeg: Double, height: Float): Mat4 {
        val c = cos(Math.toRadians(pitchDeg)).toFloat(); val s = sin(Math.toRadians(pitchDeg)).toFloat()
        // GL camera: +x right, +y up, -z forward. Forward pitched down = (0, -s, -c), so z = (0, s, c).
        return Mat4.fromColumns(Vec3(1f, 0f, 0f), Vec3(0f, c, -s), Vec3(0f, s, c), Vec3(0f, height, 0f))
    }

    /**
     * `noiseSigma`: lift noise added to every point, shaped like the phone's (gt-1791624180.csv):
     * Gaussian along the ray with this sigma and a third of it across (bearing is good, range is
     * not), and every 8th hit of a person 60 % farther along the ray (legs hidden behind
     * furniture: the ray to the box bottom meets the floor behind them). 0 is the clip's own
     * ray-to-floor lift (feet visible, ~0.1 m); 0.3 is the width prior at 1-3 m, as in a room.
     */
    /**
     * `export`: when given, one line per box of every detection frame, `frame,i,id,moving`, with
     * the verdict the phone's overlay would show for it (its track's MOVING at the next tick, or
     * the near-a-mover rule for a box without a track), so `tools/movers/render_replay.py` can
     * draw the chain's verdicts back onto the footage.
     */
    private fun replay(clip: Clip, stride: Int, config: Tracker.Config, noiseSigma: Float = 0f, export: java.io.PrintWriter? = null): Run {
        val rng = java.util.Random(7)
        val m = clip.meta
        // The clip's ground fit (vtest.avi: ~85 deg horizontal field of view, 42.2 deg down, 5.1 m up).
        val k = Lift3D.Intrinsics(fx = m.f, fy = m.f, cx = m.width / 2f, cy = m.height / 2f, width = m.width, height = m.height)
        val cam = camera(m.pitchDeg, m.height_m)
        val origin = Origin().apply { observeHorizontalPlane(0f, isFloor = true); lockManual(cam) }
        val geo = Lift3D.FrameGeometry(cam, k, origin.floorY)
        val tracker = Tracker(config)
        val motion = OverlayMotion()
        val eye = origin.toMarker(cam.origin)

        // Detection frames: every `stride`-th of the frames the clip has boxes for (a footage run
        // holds every 6th frame of a 30 fps clip already).
        val present = clip.frames.keys.toList()
        val lastFrame = present.last()
        val detFrames = present.filterIndexed { i, _ -> i % stride == 0 }.iterator()
        var nextDet = detFrames.next()
        val confirmed = HashSet<Int>(); val moving = HashSet<Int>()
        val firstSeen = HashMap<Int, Double>(); val lastSeen = HashMap<Int, Double>()
        val speeds = ArrayList<Float>()
        val maxSpeed = HashMap<Int, Float>()
        var detBoxes = 0; var detCalls = 0
        class Pending(val frame: Int, val point: WorldPoint, val id: Int?)
        val pending = ArrayList<Pending>()
        export?.println("frame,i,id,moving")
        val tickDt = 1.0 / 30
        var t = 0.0
        val end = (lastFrame + 1) / m.fps
        while (t <= end) {
            while (nextDet / m.fps <= t) {
                val dets = clip.frames[nextDet] ?: emptyList()
                var points = Lift3D.lift(dets, geo, null, origin, m.maxRange)
                // The phone's lift noise (see above), and, as Lift3D does, the point says its sigma.
                if (noiseSigma > 0f) points = points.map { p ->
                    val eye = p.eye ?: origin.toMarker(cam.origin)
                    val ray = Vec3(p.pos.x - eye.x, 0f, p.pos.z - eye.z)
                    val range = ray.length
                    val u = if (range > 1e-3f) ray * (1f / range) else Vec3(0f, 0f, -1f)
                    val side = Vec3(-u.z, 0f, u.x)
                    var along = (rng.nextGaussian() * noiseSigma).toFloat()
                    if (p.classId == TrackedClass.PERSON && rng.nextInt(8) == 0) along += 0.6f * range
                    val across = (rng.nextGaussian() * noiseSigma / 3).toFloat()
                    p.copy(pos = p.pos + u * along + side * across, sigma = noiseSigma)
                }
                val ids = tracker.update(points, nextDet / m.fps)
                if (export != null) for ((p, id) in points.zip(ids)) pending.add(Pending(nextDet, p, id))
                detBoxes += dets.size; detCalls += 1
                if (!detFrames.hasNext()) { nextDet = Int.MAX_VALUE; break }
                nextDet = detFrames.next()
            }
            val tracks = tracker.tracks(t)
            val mv = motion.update(tracks, t, eye)
            if (export != null && pending.isNotEmpty()) {
                // As Pipeline.overlay does: a box with a track carries its tag; one without shows it
                // when its lifted point sits within NEAR_MOVER_M of a mover of its class.
                val movers = tracks.filter { it.id in mv }
                for (q in pending) {
                    val moving = if (q.id != null) q.id in mv else movers.any { mo ->
                        mo.classId == q.point.classId && run { val dx = mo.pos.x - q.point.pos.x; val dz = mo.pos.z - q.point.pos.z; dx * dx + dz * dz <= Pipeline.NEAR_MOVER_M * Pipeline.NEAR_MOVER_M }
                    }
                    export.println("${q.frame},${q.point.detectionIndex},${q.id ?: ""},${if (moving) 1 else 0}")
                }
                pending.clear()
            }
            for (tr in tracks) {
                confirmed.add(tr.id); firstSeen.putIfAbsent(tr.id, t); lastSeen[tr.id] = t
                if (tr.id in mv) { moving.add(tr.id); speeds.add(tr.vel.length) }
                maxSpeed[tr.id] = maxOf(maxSpeed[tr.id] ?: 0f, tr.vel.length)
            }
            t += tickDt
        }
        speeds.sort()
        val median = if (speeds.isEmpty()) 0f else speeds[speeds.size / 2]
        val lives = confirmed.map { lastSeen.getValue(it) - firstSeen.getValue(it) }.sorted()
        val medianLife = if (lives.isEmpty()) 0.0 else lives[lives.size / 2]
        val detHz = if (detCalls > 1) (detCalls - 1) / ((lastFrame - present.first()) / m.fps) else 0.0
        val walkers = moving.count { (maxSpeed[it] ?: 0f) >= 0.8f }
        return Run(detHz, noiseSigma, confirmed, moving, median, medianLife, detBoxes.toDouble() / maxOf(detCalls, 1), walkers)
    }

    private val clip by lazy { load("/replay/vtest-yolov8n-256.csv") ?: error("missing test resource (run tools/movers/phone_detect.py)") }

    /**
     * Drone footage (`tools/movers/footage_boxes.py` on a MEVA UAV clip: a DJI Inspire ~60 m up,
     * 4K at 30 fps, people 15-35 px tall, detected on tiles every 6th frame): the same chain on
     * the geometry the project is meant for. People walking across a parking lot must be movers
     * at a walking speed; the clip is unlabelled, so the checks are those sanity bounds.
     */
    @Test fun droneClipWalkersAreMovers() {
        val drone = load("/replay/drone-meva.csv")
        org.junit.Assume.assumeTrue("no drone replay fixture (run tools/movers/footage_boxes.py)", drone != null)
        // MINBAND_REPLAY_EXPORT=<file>: the per-box verdicts, for tools/movers/render_replay.py.
        val exportPath = System.getenv("MINBAND_REPLAY_EXPORT")?.takeIf { it.isNotBlank() }
        val export = exportPath?.let { java.io.PrintWriter(java.io.File(it).bufferedWriter()) }
        val r = try { replay(drone!!, 1, Tracker.Config.SLOW_DETECTOR, export = export) } finally { export?.close() }
        println("drone: $r")
        if (exportPath != null) println("verdicts exported to $exportPath")
        // Unlabelled; the bounds come from `track.py track` on the same run (100 person tracks,
        // 38 walkers of >= 3 m at a median 0.68 m/s: a parking lot where most people stand or
        // amble) and from this chain's first pass (248 ids, 127 movers, 90 of them at a walking
        // pace at some point). A regression that loses the walkers or tags everyone shows here.
        assertTrue("walkers tracked and moving: $r", r.walkers >= 30)
        assertTrue("not everyone is a mover: $r", r.moving.size <= r.confirmed.size * 0.7)
        assertTrue("movers move: $r", r.medianMovingSpeed >= 0.15f)
        assertTrue("tracks live: $r", r.medianLifeS >= 2.0)
    }

    /** Strides at 10 fps: 10 Hz (design), 3.3 Hz and 2.5 Hz (the phone, measured), 1.25 Hz (hot). */
    private val strides = listOf(1, 3, 4, 8)

    @Test fun walkersGetTracksAndMovingAtEveryDetectorRate() {
        val runs = strides.map { replay(clip, it, Tracker.Config.SLOW_DETECTOR) }
        runs.forEach { println(it) }
        val design = runs.first()
        assertTrue("the design rate must track most of the 14 walkers: $design", design.moving.size >= 8)
        assertTrue("walkers at ~1.3 m/s: $design", design.medianMovingSpeed in 0.7f..2.5f)
        for (r in runs.drop(1)) {
            assertTrue("a slow detector must not lose the walkers: $r (design: $design)",
                r.moving.size >= (design.moving.size * 0.6).toInt() && r.moving.size >= 6)
            assertTrue("moving tracks must still be walkers, not jitter: $r", r.medianMovingSpeed in 0.5f..3f)
        }
    }

    /**
     * The phone in a room (gt-1791622538.csv, 137 s): 112 confirmed tracks for 3-4 objects, people's
     * tracks living 1.4-2.0 s each. Birth from two jittery hits leaves the velocity wrong by
     * ~jitter/dt; the next detection lands outside a fixed 0.7 m gate around the prediction, a new
     * tentative track is born instead, and the old one coasts to its death. Every new id restarts
     * OverlayMotion's history and no box carries the coasting id, so MOVING never shows. The gate
     * must widen with the prediction's own uncertainty.
     */
    @Test fun noisyLiftAtThePhonesRateKeepsOneIdPerWalker() {
        val clean = replay(clip, 4, Tracker.Config.SLOW_DETECTOR)
        for (stride in listOf(4, 8)) {
            val r = replay(clip, stride, Tracker.Config.SLOW_DETECTOR, noiseSigma = 0.3f)
            println(r)
            assertTrue("ids must not churn: $r (clean: $clean)", r.confirmed.size <= clean.confirmed.size * 2)
            assertTrue("a walker's track must outlive the 2 s window: $r", r.medianLifeS >= 3.0)
            assertTrue("walkers still MOVING: $r", r.moving.size >= 6)
        }
    }

    @Test fun phoneConfigBeatsTheDefaultOnASlowDetector() {
        // The default (iOS) birth/death times assume 12 Hz; at 2.5 Hz a tentative track dies before
        // its second hit and nothing is ever born. This is the failure the phone showed first.
        val slow = replay(clip, 4, Tracker.Config.SLOW_DETECTOR)
        val fast = replay(clip, 4, Tracker.Config())
        println("stride 4, default config: $fast")
        assertTrue("$slow vs $fast", slow.confirmed.size > fast.confirmed.size)
    }
}
