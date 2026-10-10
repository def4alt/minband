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

/** Tracker output, one per confirmed track. Marker frame, metres and m/s; `conf` 0..255. */
data class Track(val id: Int, val classId: Int, val pos: Vec3, val vel: Vec3, val conf: Int)

/**
 * A lifted detection: 3D point in the marker frame (Y up). `detectionIndex` points back into the
 * detections it came from so the overlay can label boxes with track ids.
 */
data class WorldPoint(val classId: Int, val pos: Vec3, val conf: Float, val detectionIndex: Int = -1)

/** A detection box ready for the overlay: `rect` in normalized view coordinates (origin top-left). */
data class OverlayBox(val id: Int, val rect: RectF, val classId: Int, val conf: Float, val trackId: Int?)

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

    /** COCO label (as written by Ultralytics exports) -> class id. */
    val byLabel: Map<String, Int> = mapOf(
        "person" to PERSON, "backpack" to BACKPACK, "handbag" to HANDBAG, "bottle" to BOTTLE,
        "cup" to CUP, "chair" to CHAIR, "tv" to TV, "laptop" to LAPTOP, "cell phone" to CELL_PHONE,
    )

    val all: Set<Int> = byLabel.values.toSet()

    fun name(id: Int): String = byLabel.entries.firstOrNull { it.value == id }?.key ?: "class $id"

    /** Hard speed cap, same values as `ClassPrior::max_speed`: people and carried objects 3 m/s, placed objects 1 m/s. */
    fun maxSpeed(id: Int): Float = when (id) {
        CHAIR, LAPTOP, TV -> 1.0f
        else -> 3.0f
    }

    /** Association gate in metres (DESIGN 3.1: per-class gating). */
    fun gate(id: Int): Float = if (id == PERSON) 0.7f else 0.4f

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
