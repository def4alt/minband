package dev.minband.android

import android.media.Image
import java.nio.ByteOrder

/**
 * bbox centre -> ray -> depth sample (median patch) or floor-plane intersection -> marker frame.
 * Port of ios/MinBand/Lift3D.swift for ARCore.
 *
 * Positions follow the viewer's convention (a person capsule is drawn *above* `pos`): a person is
 * reported at their feet.
 * - Depth (ARCore Depth API, `Frame.acquireDepthImage16Bits`): median of a 5x5 patch at the bbox
 *   centre, zero (invalid) samples ignored, unprojected with the CPU image intrinsics. For a person
 *   the centre is the torso (the most reliable pixel), and the point is dropped to the floor height
 *   (lowest detected plane, else the marker plane y = 0) to get the feet.
 * - No depth (or no valid sample): the ray through the bbox centre, and for a person through the
 *   bottom-centre (where the feet meet the floor; "bottom" is gravity-down in the image, so it
 *   works for any device roll), intersected with the detected floor plane. ARKit raycasts against
 *   estimated planes here; ARCore's hit test needs the live frame on the GL thread, so the floor
 *   plane (which the pipeline tracks anyway) stands in for it.
 * - Neither: the detection is dropped.
 *
 * Everything the lift needs from a frame is copied into [FrameGeometry] on the GL thread, so the
 * work can run on the detection thread after the frame is gone.
 */
object Lift3D {
    const val PATCH_RADIUS = 2          // 5x5
    const val MIN_VALID_SAMPLES = 5     // of 25
    val DEPTH_RANGE = 0.15f..8.0f
    /** A person whose bottom edge is this close to the image border has no visible feet. */
    const val BORDER_MARGIN = 0.015f

    /** Camera intrinsics of the CPU image (pixels), ARCore `CameraIntrinsics`. */
    data class Intrinsics(val fx: Float, val fy: Float, val cx: Float, val cy: Float, val width: Int, val height: Int)

    /** What one frame contributes: camera -> world, intrinsics, and the world floor height if known. */
    class FrameGeometry(val cameraToWorld: Mat4, val intrinsics: Intrinsics, val floorY: Float?)

    fun lift(dets: List<Detection>, geo: FrameGeometry, depth: DepthSampler?, origin: Origin): List<WorldPoint> {
        if (dets.isEmpty()) return emptyList()
        val cam = geo.cameraToWorld
        val k = geo.intrinsics
        val down = downDirectionInImage(cam)
        val floorInMarker = origin.floorHeightInMarker
        val camPos = cam.origin

        val out = ArrayList<WorldPoint>(dets.size)
        for ((i, d) in dets.withIndex()) {
            val isPerson = d.classId == TrackedClass.PERSON
            val centre = Vec2(d.bbox.midX, d.bbox.midY)
            var world: Vec3? = null
            var fromDepth = false

            val z = depth?.median(centre, PATCH_RADIUS, MIN_VALID_SAMPLES)
            if (z != null && z in DEPTH_RANGE) {
                val pc = unproject(centre, z, k)
                world = cam.transformPoint(pc)
                fromDepth = true
            } else if (isPerson) {
                // Feet visible and a floor known: ray to the floor at the feet. Otherwise (a
                // person close enough that the box is cut at the bottom, or no floor yet) the
                // distance from the box width and a 0.55 m shoulder-width prior, along the ray
                // through the box centre. +-30 % at 1-4 m, which beats dropping the person.
                val feet = bottomPoint(d.bbox, down, k.width.toFloat(), k.height.toFloat())
                val feetVisible = feet.x >= BORDER_MARGIN && feet.y >= BORDER_MARGIN && feet.x <= 1f - BORDER_MARGIN && feet.y <= 1f - BORDER_MARGIN
                if (feetVisible && geo.floorY != null) world = rayToFloor(feet, k, cam, geo.floorY)
                if (world == null) {
                    val widthPx = boxWidthPx(d.bbox, down, k)
                    val dist = if (widthPx > 1f) k.fx * PERSON_WIDTH_M / widthPx else 0f
                    if (dist in DEPTH_RANGE) world = cam.transformPoint(unproject(centre, dist, k))
                }
                if (world != null && world.distance(camPos) > DEPTH_RANGE.endInclusive) world = null
            } else if (geo.floorY != null) {
                world = rayToFloor(centre, k, cam, geo.floorY)
                if (world != null && world.distance(camPos) > DEPTH_RANGE.endInclusive) world = null
            }
            val w = world ?: continue
            var m = origin.toMarker(w)
            if (isPerson) {
                // Depth hit the torso: drop to the floor. A floor intersection is already at the
                // feet; snap it to the known floor height too so both paths agree.
                if (floorInMarker != null) m = Vec3(m.x, floorInMarker, m.z) else if (fromDepth) m = Vec3(m.x, 0f, m.z)
            }
            out.add(WorldPoint(d.classId, m, d.conf, i))
        }
        return out
    }

    // Pure helpers (unit-tested).

    /**
     * Normalized image point + z-depth -> point in camera space (x right, y up, z backward, in
     * the sensor's landscape frame). Intrinsics are for `k.width x k.height` pixels.
     */
    fun unproject(p: Vec2, z: Float, k: Intrinsics): Vec3 {
        val u = p.x * k.width; val v = p.y * k.height
        val x = (u - k.cx) / k.fx * z
        val y = (v - k.cy) / k.fy * z
        // Pinhole (x right, y down, z forward) -> GL camera (x right, y up, z backward).
        return Vec3(x, -y, -z)
    }

    /** Shoulder-width prior for the no-floor distance estimate (tools/footage uses the same). */
    const val PERSON_WIDTH_M = 0.55f

    /** Box extent in pixels across gravity (a person's width, whichever way the sensor is turned). */
    fun boxWidthPx(box: RectF, down: Vec2, k: Intrinsics): Float {
        val wPx = box.width * k.width; val hPx = box.height * k.height
        // `down` is a unit pixel direction; the across-gravity extent weights the two sides.
        return absf(down.y) * wPx + absf(down.x) * hPx
    }

    /** Unit direction (in pixels: x right, y down) of world gravity in the captured image. */
    fun downDirectionInImage(cameraToWorld: Mat4): Vec2 {
        // World down in camera axes = R^T * (0, -1, 0): the y row... R^T rows are R's columns.
        val cx = cameraToWorld.x; val cy = cameraToWorld.y; val cz = cameraToWorld.z
        val dc = Vec3(-cx.y, -cy.y, -cz.y)           // R^T * (0,-1,0)
        val d = Vec2(dc.x, -dc.y)                     // camera y up -> image y down
        val l = d.length
        // Looking straight up/down: fall back to portrait "down" (+x of the sensor image).
        return if (l < 1e-3f) Vec2(1f, 0f) else Vec2(d.x / l, d.y / l)
    }

    /** Point just inside `box` from its centre along `down` (the feet of an upright person). Normalized coordinates in, normalized out; `down` is a unit pixel direction, so the box is measured in pixels. */
    fun bottomPoint(box: RectF, down: Vec2, imageWidth: Float = 1f, imageHeight: Float = 1f): Vec2 {
        val c = Vec2(box.midX * imageWidth, box.midY * imageHeight)
        val hw = box.width * imageWidth / 2f; val hh = box.height * imageHeight / 2f
        val tx = if (absf(down.x) > 1e-6f) hw / absf(down.x) else Float.POSITIVE_INFINITY
        val ty = if (absf(down.y) > 1e-6f) hh / absf(down.y) else Float.POSITIVE_INFINITY
        val p = c + down * (minOf(tx, ty) * 0.97f)
        return Vec2(p.x / imageWidth, p.y / imageHeight)
    }

    /** Ray from the camera through normalized image point `p`, intersected with the horizontal plane `y = floorY` (world). Null if the ray does not go down to it within range. */
    fun rayToFloor(p: Vec2, k: Intrinsics, cameraToWorld: Mat4, floorY: Float): Vec3? {
        val dirCam = unproject(p, 1f, k)
        val dir = cameraToWorld.transformDirection(dirCam)
        val o = cameraToWorld.origin
        if (dir.y > -1e-4f) return null                 // level or looking up: never meets the floor
        val t = (floorY - o.y) / dir.y
        if (t <= 0f) return null
        return o + dir * t
    }

    fun median(v: FloatArray, n: Int): Float? {
        if (n == 0) return null
        java.util.Arrays.sort(v, 0, n)
        return if (n % 2 == 1) v[n / 2] else (v[n / 2 - 1] + v[n / 2]) / 2f
    }
}

/**
 * Reads an ARCore DEPTH16 image (millimetres, little-endian, same orientation and field of view
 * as the CPU camera image) copied out of the frame, and returns robust patch medians in metres.
 * Zero means no estimate.
 */
class DepthSampler private constructor(private val depth: ShortArray, private val width: Int, private val height: Int) {

    fun median(p: Vec2, radius: Int, minValid: Int): Float? {
        val cx = (p.x * width).toInt(); val cy = (p.y * height).toInt()
        val x0 = maxOf(0, cx - radius); val x1 = minOf(width - 1, cx + radius)
        val y0 = maxOf(0, cy - radius); val y1 = minOf(height - 1, cy + radius)
        if (x0 > x1 || y0 > y1) return null
        val samples = FloatArray((2 * radius + 1) * (2 * radius + 1))
        var n = 0
        for (y in y0..y1) for (x in x0..x1) {
            val mm = depth[y * width + x].toInt() and 0xFFFF
            if (mm > 0) samples[n++] = mm / 1000f
        }
        if (n < minValid) return null
        return Lift3D.median(samples, n)
    }

    companion object {
        /** Copies the depth plane so the image can be closed right away. Null for anything but DEPTH16. */
        fun from(image: Image?): DepthSampler? {
            if (image == null || image.format != android.graphics.ImageFormat.DEPTH16) return null
            val plane = image.planes[0]
            val w = image.width; val h = image.height
            val buf = plane.buffer.order(ByteOrder.LITTLE_ENDIAN)
            val rowStride = plane.rowStride; val pixelStride = plane.pixelStride
            val out = ShortArray(w * h)
            for (y in 0 until h) {
                val row = y * rowStride
                for (x in 0 until w) out[y * w + x] = buf.getShort(row + x * pixelStride)
            }
            return DepthSampler(out, w, h)
        }

        fun of(depth: ShortArray, width: Int, height: Int) = DepthSampler(depth, width, height)
    }
}
