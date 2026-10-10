package dev.minband.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Port of ios/MinBandTests/OriginTests.swift. */
class OriginTest {
    private fun assertNear(a: Vec3, b: Vec3, accuracy: Float = 1e-4f) = assertTrue("$a != $b", a.distance(b) < accuracy)
    private fun v(x: Float, y: Float, z: Float) = Vec3(x, y, z)
    private fun yaw(angle: Float) = Quat.axisAngle(Vec3.UP, angle)
    /** Augmented-image style transform: rotation `r` (columns = image X, normal Y, height Z), origin `o`. */
    private fun anchor(r: Quat, o: Vec3) = Mat4.fromRotation(r, o)

    @Test fun unlockedIsIdentityAndNotLocked() {
        val o = Origin()
        assertFalse(o.isLocked)
        assertNear(o.toMarker(v(1f, 2f, 3f)), v(1f, 2f, 3f))
    }

    @Test fun flatMarkerWithYaw() {
        val o = Origin()
        val q = yaw(0.7f)
        val center = v(2f, -1.2f, -3f)
        o.lock(anchor(q, center))
        assertTrue(o.isLocked)
        assertEquals(Origin.Source.MARKER, o.source)
        val ax = q.act(v(1f, 0f, 0f)); val az = q.act(v(0f, 0f, 1f))
        assertNear(o.toMarker(center), Vec3.ZERO)
        assertNear(o.toMarker(center + ax), v(1f, 0f, 0f))
        assertNear(o.toMarker(center + v(0f, 1f, 0f)), v(0f, 1f, 0f))
        assertNear(o.toMarker(center + az), v(0f, 0f, 1f))
        assertNear(v(1f, 0f, 0f).cross(v(0f, 1f, 0f)), v(0f, 0f, 1f))
    }

    @Test fun tiltedMarkerIsLevelledToGravity() {
        val o = Origin()
        val r = yaw(0.3f) * Quat.axisAngle(v(1f, 0f, 0f), 4f * Math.PI.toFloat() / 180f)
        val c = v(0.5f, 0f, 0.5f)
        o.lock(anchor(r, c))
        assertNear(o.toMarker(c + v(0f, 2f, 0f)), v(0f, 2f, 0f))
        val p = o.toMarker(c + yaw(0.3f).act(v(3f, 0f, 0f)))
        assertNear(p, v(3f, 0f, 0f), 1e-3f)
    }

    @Test fun wallMarkerStillYUp() {
        val o = Origin()
        val r = Quat.axisAngle(v(1f, 0f, 0f), Math.PI.toFloat() / 2f)
        assertTrue(r.act(v(0f, 1f, 0f)).distance(v(0f, 0f, 1f)) < 1e-5f)
        o.lock(anchor(r, v(0f, 1.5f, 0f)))
        assertNear(o.toMarker(v(0f, 2.5f, 0f)), v(0f, 1f, 0f))
        assertNear(o.toMarker(v(1f, 1.5f, 0f)), v(1f, 0f, 0f))
    }

    @Test fun rotationToMarkerMatchesPointTransform() {
        val o = Origin()
        val r = yaw(-1.1f)
        o.lock(anchor(r, v(4f, 0f, 1f)))
        val q = o.rotationToMarker()
        val d = v(0.3f, -0.2f, 0.9f)
        assertNear(q.act(d), o.toMarkerDirection(d))
        assertNear(q.act(d), o.toMarker(v(4f, 0f, 1f) + d))
        val cam = Quat.axisAngle(v(1f, 1f, 0f).normalized(), 0.4f)
        val inMarker = q * cam
        assertNear(inMarker.act(v(0f, 0f, -1f)), o.toMarkerDirection(cam.act(v(0f, 0f, -1f))))
        val t = Mat4.fromRotation(cam, v(1f, 1.4f, 2f))
        val tm = o.toMarker(t)
        assertNear(tm.origin, o.toMarker(v(1f, 1.4f, 2f)))
    }

    @Test fun manualLockUsesFloorAndCameraRight() {
        val o = Origin()
        // Portrait, upright, looking along world -Z: camera +X = device bottom (world down),
        // +Y = device right (world +X), +Z = backward (world +Z).
        var cam = Mat4.fromColumns(v(0f, -1f, 0f), v(1f, 0f, 0f), v(0f, 0f, 1f), v(1f, 1.5f, 2f))
        o.observeHorizontalPlane(-0.1f, isFloor = true)
        o.lockManual(cam)
        assertEquals(Origin.Source.MANUAL, o.source)
        assertNear(o.toMarker(v(1f, -0.1f, 2f)), Vec3.ZERO)
        assertNear(o.toMarker(v(2f, -0.1f, 2f)), v(1f, 0f, 0f))
        assertNear(o.toMarker(v(1f, -0.1f, 1f)), v(0f, 0f, -1f))
        assertEquals(0f, o.floorHeightInMarker ?: 99f, 1e-5f)

        // Looking straight down still gives a level frame.
        val down = Quat.axisAngle(v(1f, 0f, 0f), -Math.PI.toFloat() / 2f)
        val upright = Quat.fromMatrix(Mat4.fromColumns(v(0f, -1f, 0f), v(1f, 0f, 0f), v(0f, 0f, 1f), Vec3.ZERO))
        cam = Mat4.fromRotation(down * upright, v(0f, 1.2f, 0f))
        val o2 = Origin()
        o2.lockManual(cam)
        assertNear(o2.toMarker(v(0f, 1.2f - Origin.DEFAULT_CAMERA_HEIGHT, 0f)), Vec3.ZERO)
        assertNear(o2.toMarker(v(0f, 2.2f - Origin.DEFAULT_CAMERA_HEIGHT, 0f)), v(0f, 1f, 0f))
    }

    @Test fun differenceAndRelockChange() {
        val o = Origin()
        val a = anchor(yaw(0f), Vec3.ZERO)
        val first = o.lock(a)
        assertFalse(first.wasLocked)
        val b = anchor(yaw(2f * Math.PI.toFloat() / 180f), v(0.03f, 0f, 0f))
        val d = o.difference(b)!!
        assertEquals(0.03f, d.first, 1e-5f)
        assertEquals(2f * Math.PI.toFloat() / 180f, d.second, 1e-3f)
        val second = o.lock(b)
        assertTrue(second.wasLocked)
        assertEquals(Origin.Source.MARKER, second.previousSource)
        o.reset()
        assertFalse(o.isLocked)
        assertNull(o.difference(b))
    }

    @Test fun floorPrefersClassifiedPlane() {
        val o = Origin()
        o.observeHorizontalPlane(-1.6f, isFloor = false)
        assertEquals(-1.6f, o.floorY)
        o.observeHorizontalPlane(-1.4f, isFloor = true)
        assertEquals(-1.4f, o.floorY)
    }
}
