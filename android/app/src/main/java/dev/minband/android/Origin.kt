package dev.minband.android

/**
 * World origin = printed marker. Converts ARCore world coordinates into the marker frame that
 * every phone (and the wire, PROTOCOL.md "metres, marker frame, Y up") shares. Port of
 * ios/MinBand/Origin.swift; the conventions are the same because ARCore and ARKit agree:
 *
 * - World: right-handed, metres, +Y up along gravity, origin and yaw arbitrary per session.
 * - `AugmentedImage.centerPose` (marker -> world): origin at the image centre, +X along the image
 *   width, +Y the image normal pointing out of the printed side, +Z along the image height toward
 *   its bottom edge. For a marker lying flat on the floor +Y is already up.
 * - Marker frame (what we output): origin at the marker centre, +Y = world up (gravity), +X = the
 *   image width axis projected onto the horizontal plane, +Z = X x Y. The tilt the tracker
 *   estimates for a flat marker is dropped on purpose: gravity from the IMU is far better than the
 *   image normal (a 1 degree tilt error puts an object 5 m away 9 cm too high or low).
 * - Manual fallback (`lockManual`): origin = camera position dropped onto the floor plane, +X =
 *   the camera's right (portrait) direction levelled, +Y up, +Z = X x Y (toward the user).
 *
 * Thread safety: read from the GL thread, the detection thread and the bridge; all state is
 * behind a lock.
 */
class Origin {
    enum class Source { NONE, MARKER, MANUAL }

    data class Change(val wasLocked: Boolean, val previousSource: Source, val translation: Float, val angle: Float)

    private val lock = Any()
    private var source_ = Source.NONE
    private var markerToWorld = Mat4.IDENTITY
    private var worldToMarker = Mat4.IDENTITY
    private var classifiedFloorY: Float? = null
    private var lowestPlaneY: Float? = null

    val isLocked: Boolean get() = synchronized(lock) { source_ != Source.NONE }
    val source: Source get() = synchronized(lock) { source_ }

    /** Floor height in world Y: the lowest plane classified as floor, else the lowest upward-facing plane seen. */
    val floorY: Float? get() = synchronized(lock) { classifiedFloorY ?: lowestPlaneY }

    /** Floor height in the marker frame (0 when the marker lies on the floor). */
    val floorHeightInMarker: Float? get() = synchronized(lock) { (classifiedFloorY ?: lowestPlaneY)?.let { it - markerToWorld.origin.y } }

    /** Marker -> world (columns: X, Y, Z axes and origin). */
    val markerTransform: Mat4 get() = synchronized(lock) { markerToWorld }

    fun reset() = synchronized(lock) {
        source_ = Source.NONE
        markerToWorld = Mat4.IDENTITY; worldToMarker = Mat4.IDENTITY
        classifiedFloorY = null; lowestPlaneY = null
    }

    /** Lock to a detected marker. `markerTransform` is the augmented image's centre pose as a matrix. */
    fun lock(markerTransform: Mat4): Change = set(levelledMarkerFrame(markerTransform), Source.MARKER)

    /** Fallback "set origin here": the camera position projected onto the detected floor plane. */
    fun lockManual(cameraTransform: Mat4): Change = set(manualFrame(cameraTransform, floorY), Source.MANUAL)

    /** How far a new marker estimate is from the current lock (null if not marker-locked). */
    fun difference(markerTransform: Mat4): Pair<Float, Float>? {
        val cur = synchronized(lock) { if (source_ == Source.MARKER) markerToWorld else null } ?: return null
        return difference(cur, levelledMarkerFrame(markerTransform))
    }

    /** Feed upward-facing horizontal plane heights (world Y; skip ceilings, tables, seats). */
    fun observeHorizontalPlane(y: Float, isFloor: Boolean) = synchronized(lock) {
        if (isFloor) classifiedFloorY = minOf(classifiedFloorY ?: y, y)
        lowestPlaneY = minOf(lowestPlaneY ?: y, y)
    }

    /** World point -> marker frame. */
    fun toMarker(p: Vec3): Vec3 = synchronized(lock) { worldToMarker }.transformPoint(p)

    /** World direction (e.g. a velocity) -> marker frame. */
    fun toMarkerDirection(v: Vec3): Vec3 = synchronized(lock) { worldToMarker }.transformDirection(v)

    /** Rotation taking world vectors into the marker frame. For a camera orientation `q` (camera -> world) the marker-frame orientation is `rotationToMarker() * q`. */
    fun rotationToMarker(): Quat = Quat.fromMatrix(synchronized(lock) { worldToMarker })

    /** Full world transform (e.g. the camera pose) -> marker frame. */
    fun toMarker(transform: Mat4): Mat4 = synchronized(lock) { worldToMarker } * transform

    private fun set(m: Mat4, src: Source): Change = synchronized(lock) {
        val was = source_
        val d = if (was == Source.NONE) Pair(Float.POSITIVE_INFINITY, Math.PI.toFloat()) else difference(markerToWorld, m)
        markerToWorld = m
        worldToMarker = m.inverseRigid()
        source_ = src
        Change(was != Source.NONE, was, d.first, d.second)
    }

    companion object {
        /** Assumed camera height above the floor when no horizontal plane has been seen yet. */
        const val DEFAULT_CAMERA_HEIGHT = 1.4f
        private val worldUp = Vec3.UP

        fun levelledMarkerFrame(t: Mat4): Mat4 {
            var x = horizontal(t.x)
            if (x.length < 0.2f) {
                // Width axis nearly vertical (marker on a wall turned 90 degrees): keep Y up, put Z
                // along the horizontal image normal, X = Y x Z.
                x = worldUp.cross(horizontal(t.y))
            }
            if (x.length < 1e-4f) x = Vec3(1f, 0f, 0f)
            return frame(t.origin, x.normalized())
        }

        fun manualFrame(c: Mat4, floorY: Float?): Mat4 {
            val cam = c.origin
            // Physical camera axes are in the landscape sensor frame: +Y_cam is the device's right
            // edge in portrait, +X_cam its bottom edge, -Z_cam the optical axis. +Y_cam stays
            // horizontal for any pitch in portrait (including looking straight down).
            var x = horizontal(c.y)
            if (x.length < 0.2f) x = horizontal(c.x)
            if (x.length < 1e-4f) x = Vec3(1f, 0f, 0f)
            val y = floorY ?: (cam.y - DEFAULT_CAMERA_HEIGHT)
            return frame(Vec3(cam.x, y, cam.z), x.normalized())
        }

        /** Translation and yaw difference between two levelled frames. */
        fun difference(a: Mat4, b: Mat4): Pair<Float, Float> {
            val dt = a.origin.distance(b.origin)
            return Pair(dt, safeAcos(a.x.dot(b.x)))
        }

        private fun frame(o: Vec3, x: Vec3): Mat4 {
            val y = worldUp
            val z = x.cross(y)
            return Mat4.fromColumns(x, y, z, o)
        }

        private fun horizontal(v: Vec3): Vec3 = v - worldUp * v.dot(worldUp)
    }
}
