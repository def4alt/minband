package dev.minband.android

// Value types shared by the perception pipeline (ios/MinBand/Models.swift). Free of ARCore so the
// tracker and its tests compile on the JVM.

/**
 * One 2D detection. `bbox` is in normalized coordinates of the CPU camera image in its native
 * (landscape sensor) orientation, origin top-left, x right, y down: the space ARCore's
 * `IMAGE_NORMALIZED` coordinates, `Camera.imageIntrinsics` (after scaling by the image size) and
 * the depth image share.
 */
data class Detection(val classId: Int, val bbox: RectF, val conf: Float)

/** Axis-aligned rectangle in normalized coordinates, origin top-left. */
data class RectF(val x: Float, val y: Float, val width: Float, val height: Float) {
    val midX get() = x + width / 2f
    val midY get() = y + height / 2f
    val maxX get() = x + width
    val maxY get() = y + height
}

/**
 * Tracker output, one per confirmed track. Marker frame, metres and m/s; `conf` 0..255. `hits`:
 * detections this track has taken (a track born from two that never gets a third is a flicker);
 * `sigma`: the noise of its last lift (m), larger far away, so a judgement on its displacement
 * can scale with it; `sigmaAcross` the same across the ray (bearing: small, unless the boxes were
 * cut by the frame's side).
 */
data class Track(val id: Int, val classId: Int, val pos: Vec3, val vel: Vec3, val conf: Int, val hits: Int = 0, val sigma: Float = 0f, val sigmaAcross: Float = 0f)

/**
 * A lifted detection: 3D point in the marker frame (Y up). `detectionIndex` points back into the
 * detections it came from so the overlay can label boxes with track ids.
 */
data class WorldPoint(
    val classId: Int, val pos: Vec3, val conf: Float, val detectionIndex: Int = -1,
    /**
     * Position noise of this lift (m, per axis; 0 = the tracker's default). The ray to a visible
     * pair of feet is ~0.1 m; a person placed by the width prior (feet out of frame) is ~20 % of
     * the range. The tracker weighs the hit and widens its gate by this.
     */
    val sigma: Float = 0f,
    /**
     * Where the camera was (marker frame) when this point was lifted, or null. With it the
     * tracker knows the ray: a lift is accurate across the ray (bearing) and poor along it
     * (range), so it gates and weighs the two directions differently.
     */
    val eye: Vec3? = null,
    /** Position noise across the ray (bearing, m; 0 = same as `sigma`): a box centre is good to a degree or two whatever the range cue did. */
    val sigmaAcross: Float = 0f,
    /**
     * The box's extent along gravity in pixels (0 = unknown). The tracker refuses a hit whose box
     * is far taller or shorter than the track's recent ones: no range change within a frame
     * explains it, a passer-by filling the view in front of the person does.
     */
    val heightPx: Float = 0f,
)

/** A detection box ready for the overlay: `rect` in normalized view coordinates (origin top-left). */
data class OverlayBox(val id: Int, val rect: RectF, val classId: Int, val conf: Float, val trackId: Int?, val moving: Boolean = false)

/** Lifted 3D point of a tracked box projected into the view, normalized, origin top-left. */
data class LiftMark(val id: Int, val trackId: Int, val x: Float, val y: Float)

/** The tracked COCO subset (ids match core/src/classes.rs) and the motion limits of the core's priors. */
object TrackedClass {
    const val PERSON = 0
    const val BACKPACK = 24
    const val HANDBAG = 26
    const val BOTTLE = 39
    const val CUP = 41
    const val CHAIR = 56
    const val TV = 62
    const val LAPTOP = 63
    const val CELL_PHONE = 67
    // Vehicles: tracked in drone footage (tools/movers replays), not by the phone's detector.
    const val CAR = 2
    const val MOTORCYCLE = 3
    const val BUS = 5
    const val TRUCK = 7

    /** COCO label (as written by Ultralytics exports) -> class id. */
    val byLabel: Map<String, Int> = mapOf(
        "person" to PERSON, "backpack" to BACKPACK, "handbag" to HANDBAG, "bottle" to BOTTLE,
        "cup" to CUP, "chair" to CHAIR, "tv" to TV, "laptop" to LAPTOP, "cell phone" to CELL_PHONE,
        "car" to CAR, "motorcycle" to MOTORCYCLE, "bus" to BUS, "truck" to TRUCK,
    )

    val all: Set<Int> = byLabel.values.toSet()

    /**
     * What the phone's detector keeps: the room classes. Vehicles are tracked by the same chain
     * on drone footage, but a phone held in a room has none to see, and a car on a poster or a
     * screen would only be a phantom.
     */
    val phone: Set<Int> = setOf(PERSON, BACKPACK, HANDBAG, BOTTLE, CUP, CHAIR, TV, LAPTOP, CELL_PHONE)

    fun vehicle(id: Int): Boolean = id == CAR || id == MOTORCYCLE || id == BUS || id == TRUCK

    /**
     * Whether a hit of class `b` may update a track of class `a`: the same class, or any two
     * vehicles (from a drone a car is a car in one frame and a truck in the next; per class that
     * was a new track at every flip, and parked cars "moved" between their duplicate tracks).
     */
    fun sameKind(a: Int, b: Int): Boolean = a == b || (vehicle(a) && vehicle(b))

    fun name(id: Int): String = byLabel.entries.firstOrNull { it.value == id }?.key ?: "class $id"

    /** Hard speed cap, same values as `ClassPrior::max_speed`: people and carried objects 3 m/s, placed objects 1 m/s. */
    fun maxSpeed(id: Int): Float = when (id) {
        CHAIR, LAPTOP, TV -> 1.0f
        CAR, MOTORCYCLE, BUS, TRUCK -> 30.0f
        else -> 3.0f
    }

    /**
     * How far per second a track may have gone between two hits before its velocity is known
     * (association only, not a cap): people and what they carry walk, placed objects stay put.
     */
    fun walkSpeed(id: Int): Float = when (id) {
        CHAIR, LAPTOP, TV -> 0f
        CAR, MOTORCYCLE, BUS, TRUCK -> 10f      // a car at town speed between two detections
        else -> 1.5f
    }

    /**
     * Typical width across (m) for the no-depth range estimate `fx * width / widthPx`: a person's
     * shoulders, a chair's seat, a laptop lid, a bottle. The only range cue for things that are
     * not on the floor (the ray through a cup on a table meets the floor far behind it).
     */
    fun widthPrior(id: Int): Float = when (id) {
        PERSON -> 0.55f
        CHAIR -> 0.45f
        LAPTOP -> 0.33f
        TV -> 0.9f
        BACKPACK -> 0.35f
        HANDBAG -> 0.3f
        BOTTLE -> 0.07f
        CUP -> 0.09f
        CELL_PHONE -> 0.075f
        CAR -> 1.8f
        MOTORCYCLE -> 0.8f
        BUS, TRUCK -> 2.5f
        else -> 0.5f
    }

    /** Classes that stand on the floor, so the bottom of their box is their ground contact. */
    fun onFloor(id: Int): Boolean = id == PERSON || id == CHAIR || vehicle(id)

    /** Placed objects: they do not move on their own, and their lifts flip. */
    fun placed(id: Int): Boolean = id == CHAIR || id == LAPTOP || id == TV

    /** Carried objects: small, on tables, placed by a width prior that swings. */
    fun carried(id: Int): Boolean = id == BACKPACK || id == HANDBAG || id == BOTTLE || id == CUP || id == CELL_PHONE

    /**
     * Detector confidence to keep a box. People lower than the rest: at 256 px a person 7 m away
     * is ~25 px tall and scores 0.25-0.35, so at 0.35 they were seen every other second and never
     * lived long enough to be born as a track; a phantom person costs two agreeing hits to matter.
     */
    fun confidence(id: Int): Float = if (id == PERSON) 0.25f else 0.35f

    /** Association gate in metres (DESIGN 3.1: per-class gating). */
    fun gate(id: Int): Float = when {
        id == PERSON -> 0.7f
        vehicle(id) -> 1.5f       // a car's box centre wanders over its 4.5 m length as it turns
        else -> 0.4f
    }

    /** COCO label -> tracked class id, null for classes we do not track. Accepts the usual spellings. */
    fun classId(label: String): Int? {
        val l = label.trim().lowercase().replace('_', ' ').replace('-', ' ')
        byLabel[l]?.let { return it }
        when (l) {
            "cellphone", "mobile phone", "phone" -> return CELL_PHONE
            "tvmonitor", "tv monitor", "television", "monitor" -> return TV
            "people", "pedestrian" -> return PERSON
        }
        l.toIntOrNull()?.let { if (it in all) return it }
        return null
    }
}
