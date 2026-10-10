package dev.minband.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure helpers of Lift3D, the detector's box mapping and the host parser. */
class Lift3DTest {
    private val k = Lift3D.Intrinsics(fx = 500f, fy = 500f, cx = 320f, cy = 240f, width = 640, height = 480)

    @Test fun unprojectCentreIsOnTheOpticalAxis() {
        val p = Lift3D.unproject(Vec2(0.5f, 0.5f), 2f, k)
        assertEquals(0f, p.x, 1e-5f); assertEquals(0f, p.y, 1e-5f); assertEquals(-2f, p.z, 1e-5f)
    }

    @Test fun unprojectRightAndDownInImageAreRightAndDownInCamera() {
        val p = Lift3D.unproject(Vec2(1f, 1f), 1f, k)   // bottom-right pixel
        assertTrue(p.x > 0f)          // right
        assertTrue(p.y < 0f)          // image down = camera -y
        assertEquals(-1f, p.z, 1e-6f)
        assertEquals((640f - 320f) / 500f, p.x, 1e-5f)
    }

    @Test fun downDirectionForUprightPortraitIsImagePlusX() {
        // Portrait, upright: camera +X = world down, +Y = world +X, +Z = world +Z.
        val cam = Mat4.fromColumns(Vec3(0f, -1f, 0f), Vec3(1f, 0f, 0f), Vec3(0f, 0f, 1f), Vec3(1f, 1.5f, 2f))
        val d = Lift3D.downDirectionInImage(cam)
        assertEquals(1f, d.x, 1e-5f); assertEquals(0f, d.y, 1e-5f)
    }

    @Test fun boxHeightAndWidthFollowGravity() {
        // Portrait phone: gravity is +x in the landscape sensor image, so a standing person's
        // height is the box's x extent and their width its y extent.
        val box = RectF(0.1f, 0.1f, 0.4f, 0.2f)
        assertEquals(0.4f * 640, Lift3D.boxHeightPx(box, Vec2(1f, 0f), k), 1e-3f)
        assertEquals(0.2f * 480, Lift3D.boxWidthPx(box, Vec2(1f, 0f), k), 1e-3f)
        // Landscape phone: the other way round.
        assertEquals(0.2f * 480, Lift3D.boxHeightPx(box, Vec2(0f, 1f), k), 1e-3f)
        assertEquals(0.4f * 640, Lift3D.boxWidthPx(box, Vec2(0f, 1f), k), 1e-3f)
    }

    @Test fun bottomPointFollowsDown() {
        val box = RectF(0.4f, 0.4f, 0.2f, 0.2f)
        val p = Lift3D.bottomPoint(box, Vec2(1f, 0f))
        assertEquals(0.4f + 0.2f * 0.97f + 0.1f * (1f - 0.97f), p.x, 0.02f)
        assertEquals(0.5f, p.y, 1e-5f)
        assertTrue(p.x < box.maxX)
    }

    // Lifts of a portrait phone (gravity along the captured image's x) 1.4 m up, level, floor at y = 0.
    private val portrait = Mat4.fromColumns(Vec3(0f, -1f, 0f), Vec3(1f, 0f, 0f), Vec3(0f, 0f, 1f), Vec3(0f, 1.4f, 0f))
    private val origin = Origin().apply { observeHorizontalPlane(0f, isFloor = true); lockManual(portrait) }
    private val geo = Lift3D.FrameGeometry(portrait, k, 0f)
    private fun lift(box: RectF) = Lift3D.lift(listOf(Detection(TrackedClass.PERSON, box, 0.9f)), geo, null, origin).single()
    private fun range(p: WorldPoint) = Vec2(p.pos.x - p.eye!!.x, p.pos.z - p.eye!!.z).length

    @Test fun feetVisibleIsJudgedOnTheBoxEdgeNotTheInsetFeetPoint() {
        // A box flush with the frame's bottom (x + w = 0.997): its feet point, 3 % inside, is past the
        // margin, but there are no feet in it. The width prior must place it (sigma >= 0.25), not a
        // floor ray at the frame's edge (sigma ~0.1, metres behind the person).
        val flush = lift(RectF(0.6f, 0.3f, 0.397f, 0.2f))
        assertTrue("width prior, sigma ${flush.sigma}", flush.sigma >= Lift3D.WIDTH_SIGMA_MIN)
        // The same box 5 % short of the edge has feet: a floor ray, a feet ray's sigma.
        val inside = lift(RectF(0.55f, 0.3f, 0.397f, 0.2f))
        assertTrue("floor ray, sigma ${inside.sigma}", inside.sigma < Lift3D.WIDTH_SIGMA_MIN)
        assertEquals(0.397f * 640, inside.heightPx, 1e-3f)
    }

    @Test fun aPersonTooTallForTheirFloorRangeIsPlacedByWidthInstead() {
        // The session's passer-by: a box over 94 % of the frame's height (602 px), bottom 5.6 % short
        // of the edge, where the floor ray says 2.7 m; at 2.7 m a person is 315 px. Feet cut off:
        // the width prior (0.26 of the frame's 480 px: 2.2 m along the ray) with its sigma, not a
        // 0.1 m feet ray.
        val tall = lift(RectF(0.003f, 0.74f, 0.941f, 0.26f))
        assertTrue("width prior, sigma ${tall.sigma}", tall.sigma >= Lift3D.WIDTH_SIGMA_MIN)
        assertEquals(500f * 0.55f / (0.26f * 480f), range(tall), 0.25f)
        // A standing person at 2.5 m is ~290 px tall: the floor ray stands.
        val far = lift(RectF(0.49f, 0.4f, 0.455f, 0.15f))
        assertTrue("floor ray, sigma ${far.sigma}", far.sigma < Lift3D.WIDTH_SIGMA_MIN)
        assertEquals(2.5f, range(far), 0.3f)
    }

    @Test fun aPersonFillingTheFrameAcrossGravityIsNotLifted() {
        // Portrait: a box 95 % of the image's 480 px across gravity (someone at the phone; such
        // lifts landed 0.01-0.4 m from the camera and walked with the holder). Dropped. At 80 % it
        // is still a person a metre away, lifted.
        assertTrue(Lift3D.lift(listOf(Detection(TrackedClass.PERSON, RectF(0.1f, 0.02f, 0.8f, 0.95f), 0.9f)), geo, null, origin).isEmpty())
        assertEquals(1, Lift3D.lift(listOf(Detection(TrackedClass.PERSON, RectF(0.1f, 0.1f, 0.8f, 0.8f), 0.9f)), geo, null, origin).size)
    }

    @Test fun rayToFloorHitsBelowTheCamera() {
        // Camera 1.5 m up looking along -Z (identity rotation), floor at y = 0.
        val cam = Mat4.fromColumns(Vec3(1f, 0f, 0f), Vec3(0f, 1f, 0f), Vec3(0f, 0f, 1f), Vec3(0f, 1.5f, 0f))
        // A pixel below the centre looks down.
        val hit = Lift3D.rayToFloor(Vec2(0.5f, 1f), k, cam, 0f)!!
        assertEquals(0f, hit.y, 1e-5f)
        assertTrue(hit.z < 0f)
        // Centre pixel is level: no hit.
        assertNull(Lift3D.rayToFloor(Vec2(0.5f, 0.5f), k, cam, 0f))
    }

    @Test fun medianOfPatch() {
        assertEquals(3f, Lift3D.median(floatArrayOf(5f, 1f, 3f), 3)!!, 0f)
        assertEquals(2.5f, Lift3D.median(floatArrayOf(4f, 1f, 3f, 2f), 4)!!, 0f)
        assertNull(Lift3D.median(FloatArray(0), 0))
    }

    @Test fun depthSamplerIgnoresZeros() {
        val w = 8; val h = 8
        val d = ShortArray(w * h) { 1500 }
        d[0] = 0
        val s = DepthSampler.of(d, w, h)
        assertEquals(1.5f, s.median(Vec2(0.5f, 0.5f), 2, 5)!!, 1e-6f)
        assertNull(s.median(Vec2(0.5f, 0.5f), 2, 26))   // never enough valid samples
    }

    @Test fun capturedImageRectUndoesPortraitRotation() {
        // Upright box at the top-left of the portrait view -> in the raw landscape image (rotated
        // 90 deg clockwise to display) it sits at the bottom-left.
        val r = YoloOnnxDetector.capturedImageRect(RectF(0f, 0f, 0.5f, 0.25f), 90)
        assertEquals(0f, r.x, 1e-6f); assertEquals(0.5f, r.y, 1e-6f)
        assertEquals(0.25f, r.width, 1e-6f); assertEquals(0.5f, r.height, 1e-6f)
        val same = YoloOnnxDetector.capturedImageRect(RectF(0.1f, 0.2f, 0.3f, 0.4f), 0)
        assertEquals(RectF(0.1f, 0.2f, 0.3f, 0.4f), same)
    }

    @Test fun hostParsing() {
        assertEquals(Pair("1.2.3.4", 7777), UdpTransport.parse("1.2.3.4"))
        assertEquals(Pair("1.2.3.4", 9000), UdpTransport.parse(" 1.2.3.4:9000 "))
        assertEquals(Pair("laptop.local", 7777), UdpTransport.parse("laptop.local"))
        assertEquals(Pair("::1", 7777), UdpTransport.parse("[::1]:7777"))
    }

    @Test fun groundTruthRowFormat() {
        val t = Track(3, TrackedClass.PERSON, Vec3(1.5f, 0f, -2.25f), Vec3(0.1f, 0f, 0f), 200)
        assertEquals("120,3,0,1.50000,0.00000,-2.25000,0.10000,0.00000,0.00000,200\n", GroundTruthLog.row(120, t))
    }
}
