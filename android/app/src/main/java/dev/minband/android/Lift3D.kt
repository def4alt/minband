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

    /** `maxRange`: farthest lift kept (m). The phone's 8 m; a drone replay passes its own. */
    fun lift(dets: List<Detection>, geo: FrameGeometry, depth: DepthSampler?, origin: Origin, maxRange: Float = DEPTH_RANGE.endInclusive): List<WorldPoint> {
        if (dets.isEmpty()) return emptyList()
        val cam = geo.cameraToWorld
        val k = geo.intrinsics
        val down = downDirectionInImage(cam)
        // The marker lies on the floor and a manual origin is dropped onto it, so y = 0 is the
        // floor until ARCore has a plane to say better; without this a person's y was their box
        // centre's height, and two hits of one walker, standing and bending, were a metre apart.
        val floorInMarker = origin.floorHeightInMarker ?: 0f
        val camPos = cam.origin
        val eye = origin.toMarker(camPos)

        val out = ArrayList<WorldPoint>(dets.size)
        for ((i, d) in dets.withIndex()) {
            val isPerson = d.classId == TrackedClass.PERSON
            val centre = Vec2(d.bbox.midX, d.bbox.midY)
            val heightPx = boxHeightPx(d.bbox, down, k)
            // A person whose box spans the frame across gravity is nearer than any lift can say
            // (a 0.55 m person fills a 480 px image at 0.6 m; the box is cut on both sides, the
            // width prior lands anywhere from the lens to 0.6 m, and with the phone tilted down
            // that is a track at the camera's feet that moves with every step). No point: the
            // box stays on the overlay, nothing is tracked until they step back.
            if (isPerson && boxWidthPx(d.bbox, down, k) >= FULL_WIDTH_FRACTION * boxWidthPx(FULL_FRAME, down, k)) continue
            var world: Vec3? = null
            var how = Lift.FLOOR

            val z = depth?.median(centre, PATCH_RADIUS, MIN_VALID_SAMPLES)
            if (z != null && z in DEPTH_RANGE) {
                val pc = unproject(centre, z, k)
                world = cam.transformPoint(pc)
                how = Lift.DEPTH
            } else if (TrackedClass.onFloor(d.classId)) {
                // Standing on the floor (a person, a chair): bottom of the box visible and a floor
                // known, the ray to the floor there (the feet, the legs). Otherwise (close enough
                // that the box is cut at the bottom, or no floor yet) the distance from the box
                // width and the class's width prior (0.55 m shoulders), along the ray through the
                // box centre. +-30 % at 1-4 m, which beats dropping the person.
                // A person whose box is squat (under SQUAT_ASPECT widths tall: seated, or standing
                // with the legs hidden behind a desk) has no feet at its bottom edge: the ray there
                // lands 1.5-2.5x too far (steadily, which is fine for someone who is not moving),
                // and the width prior swings as the box takes in arms or torso. Either lift is kept
                // but with a large sigma, so the tracker barely moves a seated person and the
                // MOVING rule asks metres of them; a walker briefly cut off is just slower to tag.
                val squat = isPerson && heightPx < SQUAT_ASPECT * boxWidthPx(d.bbox, down, k)
                val feet = bottomPoint(d.bbox, down, k.width.toFloat(), k.height.toFloat())
                // Visibility is judged on the box's own edge: the feet point sits 3 % inside it,
                // which for a box spanning the frame is past the margin, and a person flush with
                // the border (legs below the frame) was placed by a floor ray at the frame's
                // bottom, metres behind them, with a feet ray's small sigma.
                val edge = bottomPoint(d.bbox, down, k.width.toFloat(), k.height.toFloat(), inset = 1f)
                val feetVisible = edge.x >= BORDER_MARGIN && edge.y >= BORDER_MARGIN && edge.x <= 1f - BORDER_MARGIN && edge.y <= 1f - BORDER_MARGIN
                if (feetVisible && geo.floorY != null) {
                    val onFloor = rayToFloor(feet, k, cam, geo.floorY)
                    // A person taller in pixels than anyone can be at the range the floor ray
                    // gives has their feet below the frame (the detector's box ended at the knees,
                    // short of the border): that range is too far, the width prior is nearer.
                    val tooTall = onFloor != null && isPerson &&
                        heightPx > TALL_RATIO * PERSON_HEIGHT_M * k.fx / maxOf(0.1f, onFloor.distance(camPos))
                    if (onFloor != null && !tooTall) { world = onFloor; if (squat) how = Lift.SQUAT }
                }
                if (world == null) world = fromWidth(d, centre, down, k, cam, maxRange)?.also { how = if (squat) Lift.SQUAT else Lift.WIDTH }
                if (world != null && world.distance(camPos) > maxRange) world = null
            } else {
                // Not on the floor (a cup, bottle or laptop on a table, a tv): the ray through the
                // box to the floor would land far behind it (1.5-3x the range from a desk), and
                // a flicker of the box then reads as metres of motion. The width prior instead;
                // the point keeps the height the centre ray gives it.
                world = fromWidth(d, centre, down, k, cam, maxRange)?.also { how = Lift.WIDTH }
                if (world != null && world.distance(camPos) > maxRange) world = null
            }
            val w = world ?: continue
            val range = w.distance(camPos)
            // A ray to the floor: a pixel of box-edge error moves the hit range^2 / (camera height
            // x focal length) along the ray (the phone at 5 m and 1.3 m up with f 500 px: 0.04 m
            // per pixel; a drone at 50 m and 30 m up with f 2100 px: 0.04 m too), and range / f
            // across it.
            val camH = maxOf(0.3f, camPos.y - (geo.floorY ?: (camPos.y - Origin.DEFAULT_CAMERA_HEIGHT)))
            val sigma = when (how) {
                Lift.DEPTH -> DEPTH_SIGMA
                Lift.FLOOR -> maxOf(FLOOR_SIGMA_MIN, range * range / (camH * k.fx) * FEET_PX)
                Lift.WIDTH -> maxOf(WIDTH_SIGMA_MIN, WIDTH_SIGMA_REL * range)
                Lift.SQUAT -> maxOf(WIDTH_SIGMA_MIN, SQUAT_SIGMA_REL * range)
            }
            var sigmaAcross = if (how == Lift.DEPTH) DEPTH_SIGMA else maxOf(ACROSS_SIGMA_MIN, range / k.fx * ACROSS_PX)
            // A box cut by the frame's side (across gravity) is centred on the visible part, not on
            // the object: as more of it leaves the frame the centre, and the bearing, slide inward
            // with the object standing still, and cut on one side then the other it swings by the
            // box's whole width. Its bearing is good only to half of whichever is wider, the class
            // or the visible box at this range (a squat box with arms out was 1.8 m at 7.5 m).
            if (sideCut(d.bbox, down, k)) {
                val visibleM = boxWidthPx(d.bbox, down, k) * range / k.fx
                sigmaAcross = maxOf(sigmaAcross, SIDE_CUT_WIDTHS * maxOf(TrackedClass.widthPrior(d.classId), visibleM))
            }
            var m = origin.toMarker(w)
            if (TrackedClass.onFloor(d.classId)) {
                // Depth hit the torso, the width prior the box centre: drop to the floor. A floor
                // intersection is already at the feet; snap it to the floor height too so all
                // paths agree.
                m = Vec3(m.x, floorInMarker, m.z)
            }
            out.add(WorldPoint(d.classId, m, d.conf, i, sigma, eye, sigmaAcross, heightPx))
        }
        return out
    }

    private enum class Lift { DEPTH, FLOOR, WIDTH, SQUAT }

    /** Range from the box width and the class's width prior, along the ray through the box centre; null when the box is degenerate or the range implausible. */
    private fun fromWidth(d: Detection, centre: Vec2, down: Vec2, k: Intrinsics, cam: Mat4, maxRange: Float): Vec3? {
        val widthPx = boxWidthPx(d.bbox, down, k)
        val dist = if (widthPx > 1f) k.fx * TrackedClass.widthPrior(d.classId) / widthPx else 0f
        return if (dist >= DEPTH_RANGE.start && dist <= maxRange) cam.transformPoint(unproject(centre, dist, k)) else null
    }

    /**
     * Position noise reported with each lift (m, per axis), so the tracker weighs a hit by how it
     * was placed and gates by it. A depth sample ~0.1 m. A ray to the feet: a few pixels of box
     * edge, which the camera geometry turns into range^2 / (height x focal) metres per pixel;
     * measured on a standing person's hits at 5-6 m in a room session, 0.35 m along the ray. The
     * width prior: +-30 % of the range at 1-4 m (people differ in width, the box in pose), so ~20 %.
     * The phone, hot, detects at 2-3 Hz; with a fixed 0.1 m the filter trusted width-prior hits
     * far too much, its velocity swung by jitter/dt, and the next detection fell outside the 0.7 m
     * gate: a new id every ~2 s per person (112 ids in a 137 s room session) and never a MOVING tag.
     */
    const val DEPTH_SIGMA = 0.1f
    const val FLOOR_SIGMA_MIN = 0.1f
    /** Box-bottom (feet) error in pixels behind a floor ray's range sigma: 8 px of a 640 px image reproduces the 0.35 m measured on a standing person 5.5 m from the phone. */
    const val FEET_PX = 8f
    const val WIDTH_SIGMA_MIN = 0.25f
    const val WIDTH_SIGMA_REL = 0.2f
    /**
     * A person box less than this many widths tall is seated or cut off (a standing adult is
     * 3-4 widths tall, 2.5 seen from the side; seated 1.1-1.8, measured in a room session where
     * the two seated people's boxes swung 0.9-7.7 m of width-prior range and were tagged MOVING).
     */
    const val SQUAT_ASPECT = 2.0f
    /** Width-prior sigma of a squat person box, as a fraction of range: its width is arms or torso at random. */
    const val SQUAT_SIGMA_REL = 0.35f
    /**
     * Noise across the ray (bearing), whatever the range cue: the box centre is good to a few
     * pixels. Measured on the room sessions, 3-5 cm between a standing person's hits at 4-6 m.
     * This is what makes a walker crossing the view visible when their range is not: the tracker
     * weighs that direction by it, not by `sigma`.
     */
    const val ACROSS_SIGMA_MIN = 0.05f
    /** Box-centre error in pixels behind the across-ray sigma (3-5 cm at 4-6 m on the phone). */
    const val ACROSS_PX = 3f
    /** Across-ray sigma of a box cut by the frame's side, in widths of its class (the centre is off by up to half). */
    const val SIDE_CUT_WIDTHS = 0.5f

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

    /** Shoulder-width prior for the no-floor distance estimate (tools/footage uses the same); the other classes are in `TrackedClass.widthPrior`. */
    const val PERSON_WIDTH_M = 0.55f
    /** A standing adult, for the "too tall for that range" check on a floor-ray lift. */
    const val PERSON_HEIGHT_M = 1.7f
    /**
     * A person box taller than this many times PERSON_HEIGHT_M at the floor ray's range has its
     * feet cut off. Measured: a walker passing the phone at ~1.5 m filled 94 % of the frame
     * (602 px) and the ray at the box's bottom gave 3.1 m, where a person is 270 px; the
     * standing person behind them took that hit and was MOVING for a second. 1.5 leaves room
     * for a 2 m person and a box with some margin.
     */
    const val TALL_RATIO = 1.5f
    /** A person box at least this fraction of the frame wide (across gravity) is not lifted: they are closer than the lift's reach. Measured: a person at the phone gave boxes 0.95-0.99 of the frame and lifts 0.01-0.4 m from the camera. */
    const val FULL_WIDTH_FRACTION = 0.9f
    private val FULL_FRAME = RectF(0f, 0f, 1f, 1f)

    /** Box extent in pixels across gravity (a person's width, whichever way the sensor is turned). */
    fun boxWidthPx(box: RectF, down: Vec2, k: Intrinsics): Float {
        val wPx = box.width * k.width; val hPx = box.height * k.height
        // `down` is a unit pixel direction; the across-gravity extent weights the two sides.
        return absf(down.y) * wPx + absf(down.x) * hPx
    }

    /** Box extent in pixels along gravity (a person's height, whichever way the sensor is turned). */
    fun boxHeightPx(box: RectF, down: Vec2, k: Intrinsics): Float {
        val wPx = box.width * k.width; val hPx = box.height * k.height
        return absf(down.x) * wPx + absf(down.y) * hPx
    }

    /**
     * True when the box touches the image border across gravity (a person cut at the left or
     * right of the view, whichever way the phone is held). Measured: a person 7.5 m out at the
     * side of the view, the box cut to 0.38 then 0.28 of their width, read as 1.4 m of sideways
     * motion in 4 s and was tagged MOVING while standing (jumping) in place.
     */
    fun sideCut(box: RectF, down: Vec2, k: Intrinsics): Boolean {
        val w = k.width.toFloat(); val h = k.height.toFloat()
        for (dir in arrayOf(Vec2(-down.y, down.x), Vec2(down.y, -down.x))) {
            val e = bottomPoint(box, dir, w, h, inset = 1f)
            if (e.x < BORDER_MARGIN || e.y < BORDER_MARGIN || e.x > 1f - BORDER_MARGIN || e.y > 1f - BORDER_MARGIN) return true
        }
        return false
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

    /**
     * Point just inside `box` from its centre along `down` (the feet of an upright person), `inset`
     * of the way to the edge (1 = the edge itself). Normalized coordinates in, normalized out;
     * `down` is a unit pixel direction, so the box is measured in pixels.
     */
    fun bottomPoint(box: RectF, down: Vec2, imageWidth: Float = 1f, imageHeight: Float = 1f, inset: Float = 0.97f): Vec2 {
        val c = Vec2(box.midX * imageWidth, box.midY * imageHeight)
        val hw = box.width * imageWidth / 2f; val hh = box.height * imageHeight / 2f
        val tx = if (absf(down.x) > 1e-6f) hw / absf(down.x) else Float.POSITIVE_INFINITY
        val ty = if (absf(down.y) > 1e-6f) hh / absf(down.y) else Float.POSITIVE_INFINITY
        val p = c + down * (minOf(tx, ty) * inset)
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
