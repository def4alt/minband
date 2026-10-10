package dev.minband.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.cos
import kotlin.math.ln
import kotlin.math.sqrt

/** Port of ios/MinBandTests/TrackerTests.swift. */
class TrackerTest {
    private val dt = 1.0 / 12.0

    private fun point(p: Vec3, cls: Int = TrackedClass.PERSON, conf: Float = 0.8f) = WorldPoint(cls, p, conf)
    private fun v(x: Float, y: Float, z: Float) = Vec3(x, y, z)

    @Test fun birthAfterThreeHits() {
        val tr = Tracker()
        tr.update(listOf(point(v(1f, 0f, 2f))), 0.0)
        assertTrue(tr.tracks(0.0).isEmpty())
        tr.update(listOf(point(v(1f, 0f, 2f))), dt)
        assertTrue("still tentative after 2 hits", tr.tracks(dt).isEmpty())
        val ids = tr.update(listOf(point(v(1f, 0f, 2f))), 2 * dt)
        val tracks = tr.tracks(2 * dt)
        assertEquals(1, tracks.size)
        assertEquals(1, tracks.first().id)
        assertEquals(TrackedClass.PERSON, tracks.first().classId)
        assertEquals(listOf(1), ids)
        assertEquals(1f, tracks.first().pos.x, 1e-3f)
    }

    @Test fun deathAfterOneSecondWithoutHits() {
        val tr = Tracker()
        for (i in 0 until 5) tr.update(listOf(point(v(0f, 0f, 1f), TrackedClass.CHAIR)), i * dt)
        val last = 4 * dt
        assertEquals("coasts for up to 1 s", 1, tr.tracks(last + 0.95).size)
        assertEquals("dead after 1 s without hits", 0, tr.tracks(last + 1.05).size)
        tr.update(emptyList(), last + 2)
        assertEquals(0, tr.confirmedCount)
    }

    @Test fun tentativeTracksDieQuickly() {
        val tr = Tracker()
        tr.update(listOf(point(v(3f, 0f, 3f))), 0.0)
        tr.update(emptyList(), 0.5)
        tr.update(listOf(point(v(3f, 0f, 3f))), 0.6)
        tr.update(listOf(point(v(3f, 0f, 3f))), 0.7)
        assertTrue("the first hit expired, so only 2 hits count", tr.tracks(0.7).isEmpty())
    }

    @Test fun idsMonotonicAndNeverReused() {
        val tr = Tracker()
        for (i in 0 until 3) tr.update(listOf(point(v(0f, 0f, 0f)), point(v(5f, 0f, 0f))), i * dt)
        assertEquals(listOf(1, 2), tr.tracks(2 * dt).map { it.id })
        tr.reset()
        val t0 = 10.0
        for (i in 0 until 3) tr.update(listOf(point(v(0f, 0f, 0f))), t0 + i * dt)
        assertEquals("ids continue after reset/death", listOf(3), tr.tracks(t0 + 2 * dt).map { it.id })
    }

    @Test fun constantVelocityEstimateWithin10Percent() {
        val tr = Tracker()
        val vel = v(0.9f, 0f, -0.8f)
        val p0 = v(-1f, 0f, 2f)
        val rng = SplitMix(7)
        val tail = ArrayList<Vec3>()
        val n = 48
        for (i in 0 until n) {
            val t = i * dt
            val noise = v(rng.gauss(), rng.gauss(), rng.gauss()) * 0.03f
            tr.update(listOf(point(p0 + vel * t.toFloat() + noise)), t)
            if (i >= n - 12) tr.tracks(t).firstOrNull()?.let { tail.add(it.vel) }
        }
        val mean = tail.fold(Vec3.ZERO) { a, b -> a + b } / tail.size.toFloat()
        val fin = tr.tracks((n - 1) * dt).first().vel
        assertTrue("mean over last second: $mean", (mean - vel).length / vel.length < 0.10f)
        assertTrue("final estimate: $fin", (fin - vel).length / vel.length < 0.10f)

        val clean = Tracker()
        for (i in 0 until n) clean.update(listOf(point(p0 + vel * (i * dt).toFloat())), i * dt)
        val vc = clean.tracks((n - 1) * dt).first().vel
        assertTrue("$vc", (vc - vel).length / vel.length < 0.02f)
    }

    @Test fun predictionExtrapolatesBetweenDetections() {
        val tr = Tracker()
        val vel = v(1f, 0f, 0f)
        for (i in 0 until 36) tr.update(listOf(point(vel * (i * dt).toFloat())), i * dt)
        val tLast = 35 * dt
        val a = tr.tracks(tLast).first().pos
        val b = tr.tracks(tLast + 0.5).first().pos
        assertEquals(0.5f, b.x - a.x, 0.05f)
    }

    @Test fun velocityClampedToClassMaxSpeed() {
        val fast = v(2f, 0f, 0f)
        val chair = Tracker(); val person = Tracker()
        for (i in 0 until 24) {
            val t = i * dt
            chair.update(listOf(point(fast * t.toFloat(), TrackedClass.CHAIR)), t)
            person.update(listOf(point(fast * t.toFloat(), TrackedClass.PERSON)), t)
        }
        val tq = 23 * dt
        assertTrue(chair.tracks(tq).first().vel.length <= 1.0f + 1e-5f)
        assertEquals(2.0f, person.tracks(tq).first().vel.length, 0.2f)
    }

    @Test fun perClassGating() {
        val tr = Tracker()
        for (i in 0 until 3) tr.update(listOf(point(v(0f, 0f, 0f), TrackedClass.PERSON), point(v(3f, 0f, 0f), TrackedClass.CUP)), i * dt)
        val t = 3 * dt
        tr.update(listOf(point(v(0.6f, 0f, 0f), TrackedClass.PERSON), point(v(3.6f, 0f, 0f), TrackedClass.CUP)), t)
        val tracks = tr.tracks(t)
        assertEquals(2, tracks.size)
        val person = tracks.first { it.classId == TrackedClass.PERSON }
        val cup = tracks.first { it.classId == TrackedClass.CUP }
        // 0.6 m in 83 ms is 7 m/s: taken (inside the person gate), but as an outlier, so the
        // track is nudged rather than moved (the robust update).
        assertTrue("person associated and moved", person.pos.x > 0.05f)
        assertEquals("cup did not associate; a new tentative track was born", 3f, cup.pos.x, 0.05f)
        val other = Tracker()
        for (i in 0 until 3) other.update(listOf(point(v(0f, 0f, 0f), TrackedClass.CUP)), i * dt)
        other.update(listOf(point(v(0.01f, 0f, 0f), TrackedClass.BOTTLE)), 3 * dt)
        assertEquals(listOf(TrackedClass.CUP), other.tracks(3 * dt).map { it.classId })
    }

    // Hits that know the camera position (the phone's lifts): gating across / along the ray.

    private val eye = Vec3(0f, 1.3f, 0f)
    private fun hit(p: Vec3, cls: Int = TrackedClass.PERSON, sigma: Float = 0.1f) = WorldPoint(cls, p, 0.8f, 0, sigma, eye)

    @Test fun sporadicRangeJumpKeepsTheIdAndTheSpeedLow() {
        // A standing person 3 m out, hits at 2.5 Hz; every 4th hit lands 2 m farther along the ray
        // (legs behind a desk: the ray to the box bottom meets the floor behind them). One id, the
        // track stays near the person, and its speed never reaches the MOVING threshold.
        val tr = Tracker(Tracker.Config.SLOW_DETECTOR)
        val ids = HashSet<Int>()
        var maxSpeed = 0f
        var t = 0.0
        for (k in 0 until 40) {
            tr.update(listOf(hit(v(0f, 0f, if (k % 4 == 3) -5f else -3f))), t)
            for (tk in tr.tracks(t)) { ids.add(tk.id); if (k >= 4) maxSpeed = maxOf(maxSpeed, tk.vel.length) }
            t += 0.4
        }
        assertEquals("one id for the whole run", 1, ids.size)
        val tk = tr.tracks(t).single()
        assertTrue("stays near the person: ${tk.pos}", tk.pos.z in -3.7f..-2.8f)
        assertTrue("speed after settling $maxSpeed", maxSpeed < 0.4f)
    }

    @Test fun aNeighbourOnAnotherBearingIsNotTakenForTheTrack() {
        // Two chairs 1 m apart, 3 m out. After both are seen twice only the right one is detected:
        // its hits must not be pulled onto the left chair's track (a wide isotropic gate did that,
        // and the left chair then "flew" at its speed cap).
        val tr = Tracker(Tracker.Config.SLOW_DETECTOR)
        val left = v(-0.5f, 0f, -3f); val right = v(0.5f, 0f, -3f)
        tr.update(listOf(hit(left, TrackedClass.CHAIR), hit(right, TrackedClass.CHAIR)), 0.0)
        tr.update(listOf(hit(left, TrackedClass.CHAIR), hit(right, TrackedClass.CHAIR)), 0.4)
        var t = 0.8
        repeat(4) { tr.update(listOf(hit(right, TrackedClass.CHAIR)), t); t += 0.4 }
        val tracks = tr.tracks(t - 0.4)
        assertEquals(0.5f, tracks.first { it.pos.x > 0f }.pos.x, 0.1f)
        assertTrue("left chair coasts in place, never dragged right: $tracks", tracks.none { it.pos.x > -0.4f && it.pos.x < 0.4f })
        assertTrue("no track moves: $tracks", tracks.all { it.vel.length < 0.1f })
    }

    @Test fun aBoxTwiceAsTallIsSomeoneElseWhateverTheRayGatesAllow() {
        // A seated person 4.5 m out whose lifts are poor (sigma 1.7 m: the along-ray gate reaches
        // metres). A passer-by fills the frame in front of them on the same bearing, lifted to
        // 3.1 m with a box 2.7x taller: not theirs, however well it fits the ray (the phone took
        // it and the seated person jumped 1.4 m, MOVING for a second). A box 15 % taller is theirs.
        val tr = Tracker(Tracker.Config.SLOW_DETECTOR)
        fun person(z: Float, sigma: Float, heightPx: Float) = WorldPoint(TrackedClass.PERSON, v(0f, 0f, z), 0.8f, 0, sigma, eye, heightPx = heightPx)
        var t = 0.0
        repeat(5) { tr.update(listOf(person(-4.5f, 1.7f, 220f)), t); t += 0.4 }
        val id = tr.tracks(t - 0.4).single().id
        assertEquals("the passer-by's box is not the seated person's hit", listOf<Int?>(null), tr.update(listOf(person(-3.1f, 0.1f, 600f)), t))
        assertEquals(-4.5f, tr.tracks(t).first { it.id == id }.pos.z, 0.1f)
        t += 0.4
        assertEquals("their own next box is", listOf<Int?>(id), tr.update(listOf(person(-4.3f, 1.7f, 250f)), t))
        // Points without a box size (the server's, older logs) are never refused for it.
        val plain = Tracker(Tracker.Config.SLOW_DETECTOR)
        repeat(3) { plain.update(listOf(person(-4.5f, 1.7f, 220f)), t); t += 0.4 }
        assertEquals(listOf<Int?>(1), plain.update(listOf(hit(v(0f, 0f, -4.4f), sigma = 1.7f)), t))
    }

    @Test fun aWalkerStillAssociatesAcrossTheRayAtTheSlowRate() {
        // 1.3 m/s sideways at 2.5 Hz is 0.52 m across the ray per hit; the second hit (velocity
        // unknown) must still be taken, and a few hits in the track follows at the right speed.
        val tr = Tracker(Tracker.Config.SLOW_DETECTOR)
        var t = 0.0
        for (k in 0 until 10) { tr.update(listOf(hit(v(-2f + 1.3f * t.toFloat(), 0f, -4f))), t); t += 0.4 }
        val tracks = tr.tracks(t - 0.4)
        assertEquals(1, tracks.size)
        assertEquals(1.3f, tracks.single().vel.length, 0.25f)
    }

    @Test fun twoConfirmedTracksOnOneSpotMergeIntoTheBetterSupported() {
        // A walker tracked for a while, then the detector adds a second box on their torso for two
        // frames: the duplicate is born and dies at once, the walker's id and hits survive.
        val tr = Tracker(Tracker.Config.SLOW_DETECTOR)
        var t = 0.0
        repeat(5) { tr.update(listOf(hit(v(0f, 0f, -3f))), t); t += 0.4 }
        repeat(2) { tr.update(listOf(hit(v(0f, 0f, -3f)), hit(v(0.2f, 0f, -3.1f))), t); t += 0.4 }
        val tracks = tr.tracks(t)
        assertEquals(1, tracks.size)
        assertEquals(1, tracks.single().id)
        assertTrue(tracks.single().hits >= 7)
    }

    @Test fun nearestNeighbourPrefersClosest() {
        val tr = Tracker()
        for (i in 0 until 3) tr.update(listOf(point(v(0f, 0f, 0f)), point(v(1f, 0f, 0f))), i * dt)
        tr.update(listOf(point(v(1.05f, 0f, 0f)), point(v(0.05f, 0f, 0f))), 3 * dt)
        val tracks = tr.tracks(3 * dt)
        assertEquals(2, tracks.size)
        assertTrue(tracks[0].pos.x < 0.2f)
        assertTrue(tracks[1].pos.x > 0.9f)
    }

    @Test fun confidenceIsSmoothedAndStable() {
        val tr = Tracker()
        val confs = HashSet<Int>()
        for (i in 0 until 60) {
            val c = if (i % 2 == 0) 0.70f else 0.76f
            tr.update(listOf(point(v(0f, 0f, 0f), conf = c)), i * dt)
            tr.tracks(i * dt).firstOrNull()?.let { confs.add(it.conf) }
        }
        assertTrue("conf output must not flicker: $confs", confs.size <= 2)
    }
}

/** Deterministic Gaussian noise for tests (same generator as the iOS tests). */
class SplitMix(private var state: Long) {
    fun next(): Long {
        state += -0x61c8864680b583ebL   // 0x9E3779B97F4A7C15
        var z = state
        z = (z xor (z ushr 30)) * -0x40a7b892e31b1a47L   // 0xBF58476D1CE4E5B9
        z = (z xor (z ushr 27)) * -0x6b2fb644ecceee15L   // 0x94D049BB133111EB
        return z xor (z ushr 31)
    }
    fun uniform(): Double = (next() ushr 11).toDouble() / (1L shl 53).toDouble()
    fun gauss(): Float {
        val u1 = maxOf(uniform(), 1e-12); val u2 = uniform()
        return (sqrt(-2 * ln(u1)) * cos(2 * Math.PI * u2)).toFloat()
    }
}
