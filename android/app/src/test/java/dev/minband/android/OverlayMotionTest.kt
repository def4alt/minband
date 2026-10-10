package dev.minband.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Mirrors server/test/movers.test.ts: the on-screen moving / static rule, in seconds. */
class OverlayMotionTest {
    private fun track(id: Int, pos: Vec3, vel: Vec3 = Vec3(0f, 0f, 0f), cls: Int = 0, hits: Int = 3, sigma: Float = 0.1f) =
        Track(id, cls, pos, vel, 200, hits, sigma)

    /** Run every 1/30 s from `from` to `to` with a scene of time (s); return the last moving set. */
    private fun run(m: OverlayMotion, from: Double, to: Double, scene: (Double) -> List<Track>, eye: Vec3? = null): Set<Int> {
        var out = m.update(scene(from), from, eye)
        var t = from + 1.0 / 30
        while (t <= to + 1e-9) { out = m.update(scene(t), t, eye); t += 1.0 / 30 }
        return out
    }

    @Test fun withTheCameraKnown_bearingMotionCountsAndRangeFlipsDoNot() {
        // A person cut off at 6 m: range from the box width swings 2 m along the ray between hits
        // (sigma 2 m), bearing holds. Not moving. A walker crossing the view at 1 m/s with the
        // same poor range moves 2 m across the ray in 2 s: moving.
        val eye = Vec3(0f, 1.3f, 0f)
        val flip = { t: Double -> listOf(track(12, Vec3(0f, 0f, if ((t * 2.5).toInt() % 2 == 0) -6f else -8f), Vec3(0f, 0f, 0.3f), sigma = 2f)) }
        assertFalse(12 in run(OverlayMotion(), 0.0, 4.0, flip, eye))
        val walker = { t: Double -> listOf(track(13, Vec3((t - 2).toFloat(), 0f, -6f), Vec3(1f, 0f, 0f), sigma = 2f)) }
        assertTrue(13 in run(OverlayMotion(), 0.0, 2.6, walker, eye))
        // Walking straight at the camera with a poor range needs the range to move 5 m: not seen.
        val closer = { t: Double -> listOf(track(14, Vec3(0f, 0f, (-6 + t).toFloat()), Vec3(0f, 0f, 1f), sigma = 2f)) }
        assertFalse(14 in run(OverlayMotion(), 0.0, 2.6, closer, eye))
    }

    @Test fun newTrackStartsStatic_walkerAtOneMetrePerSecondMovesWithinOneAndAHalfSeconds() {
        val m = OverlayMotion()
        val walker = { t: Double -> listOf(track(1, Vec3(t.toFloat(), 0f, 0f), Vec3(1f, 0f, 0f))) }
        assertFalse(1 in m.update(walker(0.0), 0.0))
        assertFalse(1 in run(m, 1.0 / 30, 0.95, walker))
        assertTrue(1 in run(m, 0.95 + 1.0 / 30, 1.6, walker))
    }

    @Test fun speedAloneIsNotMotion() {
        // A seated person placed by the width prior: born with 0.7 m/s from two noisy hits, position
        // jittering 0.2 m around the same spot. Never MOVING, however long the velocity lingers.
        val m = OverlayMotion()
        val seated = { t: Double -> listOf(track(11, Vec3(0.2f * ((t * 7).toInt() % 3 - 1), 0f, -2f), Vec3(0.7f, 0f, 0f), sigma = 0.3f)) }
        assertFalse(11 in run(m, 0.0, 6.0, seated))
    }

    @Test fun aTwoHitFlickerIsNeverMoving_theThirdHitUnlocks() {
        // Born from two hits, carries 1.2 m/s (what the tracker reports for a flicker): with two
        // hits it never counts, however long it lives; a third hit and it is a walker.
        val m = OverlayMotion()
        val flicker = { t: Double -> listOf(track(8, Vec3((1.2 * t).toFloat(), 0f, 0f), Vec3(1.2f, 0f, 0f), hits = 2)) }
        assertFalse(8 in run(m, 0.0, 2.0, flicker))
        val walker = { t: Double -> listOf(track(8, Vec3((1.2 * t).toFloat(), 0f, 0f), Vec3(1.2f, 0f, 0f), hits = 3)) }
        assertTrue(8 in run(m, 2.0 + 1.0 / 30, 2.2, walker))
    }

    @Test fun aFarPersonsRangeFlipNeedsMoreThanHalfAMetre() {
        // 6 m away the lift's sigma is 0.35 m and a standing person's range flips by 0.8 m between
        // hits (0.4 m/s to the filter): 2.5 sigma = 0.9 m is needed there. A walker covers it.
        val flip = { t: Double -> listOf(track(9, Vec3(0f, 0f, if ((t * 2.5).toInt() % 2 == 0) -6f else -6.8f), Vec3(0f, 0f, 0.3f), sigma = 0.35f)) }
        assertFalse(9 in run(OverlayMotion(), 0.0, 4.0, flip))
        val walker = { t: Double -> listOf(track(10, Vec3((0.8 * t).toFloat(), 0f, -6f), Vec3(0.3f, 0f, 0f), sigma = 0.35f)) }
        assertTrue(10 in run(OverlayMotion(), 0.0, 3.0, walker))
    }

    @Test fun slowDriftBelowTheSpeedRuleStillMoves() {
        val m = OverlayMotion()
        // 0.3 m/s, reported a little low (the filter lags) and under SPEED_MIN: the window must
        // catch it (the robust positions trail the raw ones by up to half a second).
        val drift = { t: Double -> listOf(track(2, Vec3((0.3 * t).toFloat(), 0f, 0f), Vec3(0.25f, 0f, 0f))) }
        assertFalse(2 in run(m, 0.0, 1.5, drift))
        assertTrue(2 in run(m, 1.5 + 1.0 / 30, OverlayMotion.MOVE_WINDOW_S + 0.7, drift))
    }

    @Test fun aLiftFlipIsNotMotion() {
        // A person placed 0.7 m farther for a while (legs hidden behind a desk) at no speed: not
        // moving. The same step with 0.3 m/s behind it is. A chair needs a metre.
        val flip = { t: Double -> listOf(track(5, Vec3(if (t < 1.0) 0f else 0.7f, 0f, 0f), Vec3(0.1f, 0f, 0f))) }
        assertFalse(5 in run(OverlayMotion(), 0.0, 3.0, flip))
        val step = { t: Double -> listOf(track(6, Vec3(if (t < 1.0) 0f else 0.7f, 0f, 0f), Vec3(0.3f, 0f, 0f))) }
        assertTrue(6 in run(OverlayMotion(), 0.0, 2.5, step))
        val chair = { t: Double -> listOf(track(7, Vec3(if (t < 1.0) 0f else 0.8f, 0f, 0f), Vec3(0.3f, 0f, 0f), 56)) }
        assertFalse(7 in run(OverlayMotion(), 0.0, 3.0, chair))
    }

    @Test fun jitteringChairNeverMoves_stoppedWalkerGoesStatic() {
        val m = OverlayMotion()
        val jitter = { t: Double -> (0.1 * Math.sin(t * 10)).toFloat() }
        val chair = { t: Double -> listOf(track(3, Vec3(1f + jitter(t), 0f, 1f - jitter(t * 1.3)), Vec3(0.05f, 0f, -0.05f), 56)) }
        assertFalse(3 in run(m, 0.0, 10.0, chair))

        val m2 = OverlayMotion()
        val walkThenStop = { t: Double ->
            if (t < 3.0) listOf(track(4, Vec3(t.toFloat(), 0f, 0f), Vec3(1f, 0f, 0f)))
            else listOf(track(4, Vec3(3f + jitter(t), 0f, 0f), Vec3(0f, 0f, 0f)))
        }
        assertTrue(4 in run(m2, 0.0, 3.0, walkThenStop))
        // STILL_S starts when the robust position enters the 0.3 m ball around the stop, which
        // trails the halt by up to a second.
        assertTrue(4 in run(m2, 3.0 + 1.0 / 30, 3.0 + OverlayMotion.STILL_S - 0.4, walkThenStop))
        assertFalse(4 in run(m2, 3.0 + OverlayMotion.STILL_S - 0.37, 3.0 + OverlayMotion.STILL_S + 1.2, walkThenStop))
    }

    @Test fun aNewIdBesideAMoverInheritsMoving() {
        // A walker (id 21) is tagged, then the tracker loses them and a new id (22) appears 0.7 m
        // from where they were, 1 s later: MOVING at once. Another new id 3 m away does not.
        val m = OverlayMotion()
        val walker = { t: Double -> listOf(track(21, Vec3(t.toFloat(), 0f, -3f), Vec3(1f, 0f, 0f))) }
        assertTrue(21 in run(m, 0.0, 3.0, walker))
        val gap = { _: Double -> emptyList<Track>() }
        run(m, 3.0 + 1.0 / 30, 4.0, gap)
        val reborn = { t: Double -> listOf(track(22, Vec3(3.2f, 0f, -3f), Vec3(0f, 0f, 0f), hits = 2), track(23, Vec3(6.5f, 0f, -3f), Vec3(0f, 0f, 0f), hits = 2), track(24, Vec3(3.2f, 0f, -3f), Vec3(0f, 0f, 0f), 56, hits = 2)) }
        val out = m.update(reborn(4.0 + 1.0 / 30), 4.0 + 1.0 / 30)
        assertTrue(22 in out)
        assertFalse(23 in out)
        assertFalse("a chair born on the walker's spot is a chair", 24 in out)
        // Gone for longer than GHOST_S: nothing to inherit.
        val m2 = OverlayMotion()
        assertTrue(21 in run(m2, 0.0, 3.0, walker))
        run(m2, 3.0 + 1.0 / 30, 3.0 + OverlayMotion.GHOST_S + 0.5, gap)
        assertFalse(22 in m2.update(reborn(6.0), 6.0))
    }

    @Test fun aSceneDraggedAlongWithTheCameraIsNotMoving() {
        // ARCore's pose drifts 1 m/s and drags the five people in view with it (their range and
        // bearing hold): the camera moved as they did, so nothing is moving. No anchors needed.
        val m = OverlayMotion()
        var t = 0.0; var out = emptySet<Int>()
        while (t <= 4.0) {
            val d = t.toFloat(); val h = 3 + (t * 2.5).toInt()
            out = m.update(listOf(track(41, Vec3(0.5f + d, 0f, -3f), Vec3(1f, 0f, 0f), hits = h), track(42, Vec3(-0.5f + d, 0f, -2.5f), Vec3(1f, 0f, 0f), hits = h), track(43, Vec3(1.5f + d, 0f, -4f), Vec3(1f, 0f, 0f), hits = h), track(44, Vec3(-1.5f + d, 0f, -3.5f), Vec3(1f, 0f, 0f), hits = h), track(45, Vec3(2.5f + d, 0f, -3f), Vec3(1f, 0f, 0f), hits = h)), t, Vec3(d, 1.3f, 0f))
            t += 1.0 / 30
        }
        assertTrue(out.isEmpty())
        // The same two people with the camera really walking past them (they stay put): still not moving.
        val m2 = OverlayMotion(); t = 0.0
        while (t <= 4.0) {
            val d = t.toFloat(); val h = 3 + (t * 2.5).toInt()
            out = m2.update(listOf(track(41, Vec3(0.5f, 0f, -3f), Vec3(0f, 0f, 0f), hits = h), track(42, Vec3(-0.5f, 0f, -2.5f), Vec3(0f, 0f, 0f), hits = h), track(43, Vec3(1.5f, 0f, -4f), Vec3(0f, 0f, 0f), hits = h), track(44, Vec3(-1.5f, 0f, -3.5f), Vec3(0f, 0f, 0f), hits = h), track(45, Vec3(2.5f, 0f, -3f), Vec3(0f, 0f, 0f), hits = h)), t, Vec3(d, 1.3f, 0f))
            t += 1.0 / 30
        }
        assertTrue(out.isEmpty())
    }

    @Test fun nothingIsJudgedWhileTheCameraFlies() {
        // The camera position jumps 3 m/s (a drifting pose); a lone track that moves with it stays static.
        val m = OverlayMotion()
        var t = 0.0; var out = emptySet<Int>()
        while (t <= 3.0) { out = m.update(listOf(track(36, Vec3(3f * t.toFloat(), 0f, -3f), Vec3(3f, 0f, 0f))), t, Vec3(3f * t.toFloat(), 1.3f, 0f)); t += 1.0 / 30 }
        assertFalse(36 in out)
        assertTrue(m.eyeSpeed > OverlayMotion.EYE_MAX_SPEED)
    }

    @Test fun medianIsComponentWise() {
        val m = OverlayMotion.median(listOf(0.0 to Vec3(3f, 0f, -1f), 0.1 to Vec3(1f, 0f, -3f), 0.2 to Vec3(2f, 0f, -2f)))
        assertEquals(2f, m.x, 0f); assertEquals(-2f, m.z, 0f)
    }

    @Test fun forgottenTracksAreDropped() {
        val m = OverlayMotion()
        m.update(listOf(track(1, Vec3(0f, 0f, 0f)), track(2, Vec3(5f, 0f, 0f))), 0.0)
        assertEquals(2, m.size)
        m.update(listOf(track(2, Vec3(5f, 0f, 0f))), 1.0)
        assertEquals("kept for KEEP_S", 2, m.size)
        m.update(listOf(track(2, Vec3(5f, 0f, 0f))), 1.0 + OverlayMotion.KEEP_S + 0.1)
        assertEquals(1, m.size)
    }
}
