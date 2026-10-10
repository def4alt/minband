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

    @Test fun bottomPointFollowsDown() {
        val box = RectF(0.4f, 0.4f, 0.2f, 0.2f)
        val p = Lift3D.bottomPoint(box, Vec2(1f, 0f))
        assertEquals(0.4f + 0.2f * 0.97f + 0.1f * (1f - 0.97f), p.x, 0.02f)
        assertEquals(0.5f, p.y, 1e-5f)
        assertTrue(p.x < box.maxX)
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
