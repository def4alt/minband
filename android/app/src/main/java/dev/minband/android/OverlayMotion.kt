package dev.minband.android

/**
 * Moving / static state of each confirmed track, for the on-screen label (the "MOVING" tag) and
 * so the box can stand out. Same rule as the server's `server/src/movers.ts`, in seconds and the
 * marker frame:
 *
 *  - the position judged is robust: the component-wise median of the track's positions over the
 *    last [ROBUST_S], taken every [ROBUST_STEP_S]. One lift flip (a person whose legs went behind
 *    a desk is placed a metre farther for one hit, 0.4 s of 30 Hz samples) does not move it.
 *  - the displacement judged is relative to the scene. ARCore's pose ran 26 m in 9 s once, with
 *    a walker filling the view, and everything in the room "moved" with it. So: if the tracks
 *    being observed (hits still coming) moved together the way the camera did, the scene is being
 *    dragged by a drifting pose and the camera's displacement is subtracted. Nothing is judged
 *    while the camera moves faster than [EYE_MAX_SPEED] (a hand does not, a drifting pose does)
 *    or while ARCore says its pose is not tracking (`poseTracking` false).
 *  - static -> moving: the robust position moved, from where it was [MOVE_WINDOW_S] ago, at
 *    least [moveDist] (its class) across the ray from the camera (bearing: the one thing a
 *    monocular lift gets right whatever hid the feet), or along it by [SIGMA_DIST] times the
 *    lift's sigma or [ALONG_RANGE] of the range if that is more; and the speed is at least
 *    [SPEED_FLOOR]. Speed alone never does it: a track born from two noisy hits (a seated
 *    person placed by the width prior) carries a finite-difference velocity that a large sigma
 *    corrects only slowly, while its position goes nowhere.
 *  - moving -> static: for the last [STILL_S] the robust position never left a [STILL_DIST] ball
 *    around its current one (so a walker who pauses keeps the tag).
 *  - a new track starts static and is not judged before its third hit ([MIN_HITS]); a walking
 *    person flips about a second after that. A new track born within [INHERIT_DIST] of a
 *    mover of its own class, alive or gone less than [GHOST_S] ago, is that mover under a new
 *    id and starts MOVING.
 *
 * Kalman velocity alone flickers around zero on a standing person (tracker noise ~10 cm), so the
 * decision uses net displacement over a window, not the instantaneous speed. The speed floor,
 * the class distances and the sigma term are the other guard: at 6 m a standing person's range
 * flips by +-0.5-0.8 m between hits, a walker moves 2.6 m in the window.
 */
class OverlayMotion {
    /** A robust sample: when, the median position, the track's hit count, lift sigma and the camera then. */
    private class Rob(val t: Double, val pos: Vec3, val hits: Int, val eye: Vec3?, val sigma: Float)

    private class State(val cls: Int) {
        val raw = ArrayDeque<Pair<Double, Vec3>>()   // (time s, position), the last ROBUST_S
        val robust = ArrayDeque<Rob>()               // every ROBUST_STEP_S, the last STILL_S
        var lastRobust = -1e9
        var moving = false
        /** When the track was last in the update; a track that blinks for a tick keeps its state and tag. */
        var lastSeen = 0.0
        /** When its hit count last grew: a mover still being observed is a second person to a new track beside it, not the same one. */
        var lastHitAt = 0.0
        var hits = 0
    }

    private val states = HashMap<Int, State>()

    /** Movers gone in the last [GHOST_S]: id, class, time last seen, last raw position. */
    private class Gone(val id: Int, val cls: Int, val t: Double, val pos: Vec3)
    private val gone = ArrayList<Gone>()

    /** The camera position a moment ago, for its speed. */
    private var lastEye: Pair<Double, Vec3>? = null
    /** Raw camera positions of the last ROBUST_S: their median is stored with each robust sample, so camera and track displacements lag alike. */
    private val eyeRaw = ArrayDeque<Pair<Double, Vec3>>()
    /** Camera speed over the last ~ROBUST_STEP_S (m/s); 0 without a camera position. */
    var eyeSpeed = 0f; private set
    /** The scene displacement subtracted at the last update (tests, logs). */
    var lastDrift: Vec3 = Vec3.ZERO; private set
    /** Set to a track id to collect one line per update about its judgement in [debugLines] (tests). */
    var debugId: Int? = null
    val debugLines = ArrayList<String>()
    /** One line per static -> moving flip since the caller last cleared it, with the numbers behind the judgement (the phone logs them: a wrong tag can then be read off logcat without a replay). */
    val flips = ArrayList<String>()

    /**
     * A new track born within [INHERIT_DIST] of a mover (alive, or gone less than [GHOST_S] ago)
     * is that mover under a new id, so it starts MOVING: the detector puts a second box on a
     * walker, the tracker loses them for a second, and the tag would otherwise start over from
     * three hits and a second of history each time.
     */
    private fun inherits(pos: Vec3, cls: Int, nowS: Double): Boolean {
        // Same class only: a chair born beside a walker is a chair, not the walker under a new id.
        // A live mover still getting hits is someone else standing beside the new track (the
        // seated neighbour the walker passes), so only a mover the detector has lost counts.
        // The last raw position, not the median: the median trails a walker by up to 1.5 s.
        for (s in states.values) if (s.moving && TrackedClass.sameKind(s.cls, cls) && nowS - s.lastHitAt > LOST_S && s.raw.isNotEmpty() && ground(s.raw.last().second, pos) <= INHERIT_DIST) return true
        for (g in gone) if (TrackedClass.sameKind(g.cls, cls) && nowS - g.t <= GHOST_S && ground(g.pos, pos) <= INHERIT_DIST) return true
        return false
    }

    private class Judged(val t: Track, val s: State, val cur: Vec3, val d: Vec3, val observed: Boolean, val eyeDisp: Vec3?, val refT: Double, val refPos: Vec3)

    /**
     * Update from the confirmed tracks at `nowS` (seconds); returns the ids currently moving.
     * `eye`: the camera position in the marker frame, so the displacement can be split into
     * across the ray from it (bearing, trusted whatever the range cue did) and along it (range,
     * which needs [SIGMA_DIST] sigma), and so a drifting pose can be told from motion. Without it
     * the displacement is judged as a whole. `poseTracking` false (ARCore lost or limited
     * tracking) holds every judgement, as a fast camera does.
     */
    fun update(tracks: List<Track>, nowS: Double, eye: Vec3? = null, poseTracking: Boolean = true): Set<Int> {
        val seen = HashSet<Int>(tracks.size)
        val moving = HashSet<Int>()
        var eyeMed: Vec3? = null
        if (eye != null) {
            val le = lastEye
            if (le == null) lastEye = Pair(nowS, eye)
            else if (nowS - le.first >= ROBUST_STEP_S) { eyeSpeed = (ground(eye, le.second) / (nowS - le.first)).toFloat(); lastEye = Pair(nowS, eye) }
            eyeRaw.addLast(Pair(nowS, eye))
            while (eyeRaw.size > 1 && eyeRaw.first().first < nowS - ROBUST_S) eyeRaw.removeFirst()
            eyeMed = median(eyeRaw)
        } else { eyeSpeed = 0f; eyeRaw.clear() }

        // Pass 1: histories, robust positions, each track's displacement over the window.
        val judged = ArrayList<Judged>(tracks.size)
        for (t in tracks) {
            seen.add(t.id)
            var s = states[t.id]
            if (s == null) {
                s = State(t.classId)
                if (inherits(t.pos, t.classId, nowS)) { s.moving = true; flips.add("id %d %s t=%.1f born within %.1f m of a lost mover -> MOVING".format(t.id, TrackedClass.name(t.classId), nowS, INHERIT_DIST)) }
                s.lastHitAt = nowS; s.hits = t.hits
                states[t.id] = s
            }
            s.lastSeen = nowS
            if (t.hits > s.hits) { s.hits = t.hits; s.lastHitAt = nowS }
            s.raw.addLast(Pair(nowS, t.pos))
            while (s.raw.size > 1 && s.raw.first().first < nowS - ROBUST_S) s.raw.removeFirst()
            if (nowS - s.lastRobust >= ROBUST_STEP_S) {
                s.lastRobust = nowS
                s.robust.addLast(Rob(nowS, median(s.raw), t.hits, eyeMed, t.sigma))
                val keepFrom = nowS - maxOf(STILL_S, MOVE_WINDOW_S) - ROBUST_STEP_S
                while (s.robust.size > 1 && s.robust.first().t < keepFrom) s.robust.removeFirst()
            }
            val cur = s.robust.last().pos
            // The medians trail the raw positions by ~ROBUST_STEP_S; the reference is older by as much.
            val ref = s.robust.firstOrNull { it.t >= nowS - MOVE_WINDOW_S - ROBUST_STEP_S } ?: s.robust.first()
            val curEye = s.robust.last().eye
            val eyeDisp = if (curEye != null && ref.eye != null) curEye - ref.eye else null
            judged.add(Judged(t, s, cur, cur - ref.pos, observed = t.hits > ref.hits, eyeDisp = eyeDisp, refT = ref.t, refPos = ref.pos))
        }

        // The scene's own displacement: only a pose drift, which drags the scene along with the
        // camera, is corrected, by the camera's own displacement. Only tracks still being
        // observed can show it (a track that coasts holds its place while a dragged scene moves
        // on). A median of the tracks' own displacements was tried as the drift and made things
        // worse: their along-ray noise became everyone's.
        val observed = judged.filter { it.observed }
        val eyeDisp = observed.firstOrNull { it.eyeDisp != null }?.eyeDisp
        val dragged = eyeDisp != null && observed.size >= DRAG_MIN_TRACKS && ground(eyeDisp, Vec3.ZERO) >= DRAG_MIN_M &&
            ground(medianOf(observed.map { it.d }), eyeDisp) <= DRAG_TOLERANCE * ground(eyeDisp, Vec3.ZERO) + 0.3f
        lastDrift = if (dragged) eyeDisp!! else Vec3.ZERO
        val poseOk = eyeSpeed <= EYE_MAX_SPEED && poseTracking

        // Pass 2: decisions.
        for (j in judged) {
            val t = j.t; val s = j.s; val cur = j.cur
            val speed = t.vel.length
            if (!s.moving) {
                // Not before MIN_HITS: a track born from two hits that never gets a third (a
                // flicker) carries their finite-difference velocity for the tracker's coast time;
                // it must never count, and a third hit comes 0.4 s after birth for a real walker.
                // And only while the track is being observed: a track the detector lost coasts on
                // its last velocity, and that extrapolation is not motion anyone saw (a person at
                // the side of the view, born from cut boxes, was tagged 2 s after their last hit).
                if (t.hits >= MIN_HITS && poseOk && nowS - s.lastHitAt <= LOST_S) {
                    val base = moveDist(t.classId)
                    // The camera's displacement over this track's own window (a track just born
                    // has none): subtracting the first track's 3 s of drift from a newborn's zero
                    // made it "move" 0.5 m the other way.
                    val drift = if (dragged) (j.eyeDisp ?: Vec3.ZERO) else Vec3.ZERO
                    val d = j.d - drift
                    val rx = if (eye != null) cur.x - eye.x else 0f
                    val rz = if (eye != null) cur.z - eye.z else 0f
                    val range = kotlin.math.sqrt(rx * rx + rz * rz)
                    // Along the ray a standing person's feet ray wanders +-0.4 m at 5 m between
                    // hits and the first hit of a track is as likely off by that: a range change
                    // counts only past ALONG_RANGE of the range (2 m at 5 m) as well as 2.5 sigma.
                    val needAlong = maxOf(base, SIGMA_DIST * t.sigma, ALONG_RANGE * range)
                    // Across the ray the bearing is trusted to its own sigma: a few centimetres for a
                    // whole box, about the object's width for one cut by the frame's side.
                    val needAcross = maxOf(base, SIGMA_DIST * t.sigmaAcross)
                    // Placed and carried objects are judged across the ray only: a chair's range
                    // steps when someone walks in front of it; a chair carried away crosses the view.
                    // So is anyone whose range is a guess (a width-prior or squat lift, sigma well
                    // above a feet ray's 6.5 % of range): a seated person's box takes in arms or
                    // torso and their range swung 1.2-2.2 m at 1.7 m.
                    // And only when every lift in the window was that good and the range changed
                    // one way: a walker approaching comes steadily closer, a flip goes and comes back.
                    val window = s.robust.filter { it.t >= j.refT }
                    val alongCounts = !(TrackedClass.placed(t.classId) || TrackedClass.carried(t.classId)) &&
                        window.count { it.sigma <= ALONG_SIGMA_REL * range } * 4 >= window.size * 3
                    val moved = if (eye != null && range > 1e-3f) {
                        val ux = rx / range; val uz = rz / range
                        val along = d.x * ux + d.z * uz
                        val ax = d.x - ux * along; val az = d.z - uz * along
                        kotlin.math.sqrt(ax * ax + az * az) >= needAcross ||
                            (alongCounts && absf(along) >= needAlong && monotoneAlong(window, j.refPos, ux, uz))
                    } else d.length >= needAlong
                    // A vehicle must also have driven: VEHICLE_MOVE_M one way, on a near-straight
                    // path, at VEHICLE_SPEED. A parked car's track wobbles back and forth (box
                    // jitter, the drone's registration, a hop to its neighbour's box and back);
                    // a driving one goes one way. Measured on a drone clip: parked cars tagged
                    // at 1.0-2.4 m within 3 s, driving ones at 1-10 m, so distance alone could
                    // not tell them apart, the path's straightness could.
                    val drove = !TrackedClass.vehicle(t.classId) || (t.hits >= VEHICLE_MIN_HITS && speed >= VEHICLE_SPEED && steady(window, j.refPos, d))
                    if (moved && drove && speed >= SPEED_FLOOR) s.moving = true
                    if (s.moving || t.id == debugId) {
                        val line = "id %d %s t=%.1f d=(%.2f,%.2f) raw=(%.2f,%.2f) drift=(%.2f,%.2f) eyeDisp=%s observed=%d dragged=%s range=%.1f needAlong=%.2f needAcross=%.2f sigma=%.2f hits=%d speed=%.2f -> %s".format(
                            t.id, TrackedClass.name(t.classId), nowS, d.x, d.z, j.d.x, j.d.z, drift.x, drift.z, eyeDisp?.let { "(%.2f,%.2f)".format(it.x, it.z) } ?: "-", observed.size, dragged, range, needAlong, needAcross, t.sigma, t.hits, speed, if (s.moving) "MOVING" else "static")
                        if (s.moving) flips.add(line)
                        if (t.id == debugId) debugLines.add(line)
                    }
                }
            } else {
                val oldest = s.robust.first()
                if (nowS - oldest.t >= STILL_S) {
                    var left = false
                    for (h in s.robust) if (h.t >= nowS - STILL_S && h.pos.distance(cur) >= STILL_DIST) { left = true; break }
                    if (!left && speed < SPEED_MIN) s.moving = false
                }
            }
            if (s.moving) moving.add(t.id)
        }

        val it = states.entries.iterator()
        while (it.hasNext()) {
            val (id, s) = it.next()
            if (id in seen || nowS - s.lastSeen <= KEEP_S) continue
            if (s.moving && s.raw.isNotEmpty()) gone.add(Gone(id, s.cls, s.lastSeen, s.raw.last().second))
            it.remove()
        }
        gone.removeAll { nowS - it.t > GHOST_S }
        return moving
    }

    /** Tracks currently held (tests / leak checks). */
    val size: Int get() = states.size

    /** The along-ray coordinate of the window's samples never steps back by more than ALONG_SLACK against the overall direction. */
    private fun monotoneAlong(window: List<Rob>, refPos: Vec3, ux: Float, uz: Float): Boolean {
        if (window.size < 2) return true
        val a = window.map { (it.pos.x - refPos.x) * ux + (it.pos.z - refPos.z) * uz }
        val dir = if (a.last() >= 0f) 1f else -1f
        for (k in 1 until a.size) if ((a[k] - a[k - 1]) * dir < -ALONG_SLACK) return false
        return true
    }

    /**
     * The window's robust positions progress one way along the net displacement `d` (never
     * stepping back by more than VEHICLE_SLACK_M) and the path is near straight (net over path
     * length at least VEHICLE_STRAIGHTNESS).
     */
    private fun steady(window: List<Rob>, refPos: Vec3, d: Vec3): Boolean {
        val net = kotlin.math.sqrt(d.x * d.x + d.z * d.z)
        if (net < 1e-3f || window.size < 2) return false
        val ux = d.x / net; val uz = d.z / net
        var path = 0f; var prev: Vec3? = null; var prevA = 0f
        for (w in window) {
            val a = (w.pos.x - refPos.x) * ux + (w.pos.z - refPos.z) * uz
            if (prev != null) {
                if (a - prevA < -VEHICLE_SLACK_M) return false
                path += ground(w.pos, prev)
            }
            prev = w.pos; prevA = a
        }
        return path < 1e-3f || net / path >= VEHICLE_STRAIGHTNESS
    }

    private fun ground(a: Vec3, b: Vec3): Float { val dx = a.x - b.x; val dz = a.z - b.z; return kotlin.math.sqrt(dx * dx + dz * dz) }

    companion object {
        const val MOVE_DIST = 0.4f
        const val MOVE_WINDOW_S = 3.0
        /** A track faster than this is not let go static (the moving -> static rule). */
        const val SPEED_MIN = 0.4f
        const val STILL_DIST = 0.3f
        const val STILL_S = 5.0
        /** Below this speed a displacement is a lift flip, not motion; a real walker's filtered speed is well above it. */
        const val SPEED_FLOOR = 0.2f
        /** A track is not called moving before this many hits (see `update`). */
        const val MIN_HITS = 3
        /** The displacement a track needs along the ray is at least this many of its lift sigma (0.35 m at 6 m: 0.9 m)... */
        const val SIGMA_DIST = 2.5f
        /** ...and at least this fraction of its range (a standing person's range wandered 4.6-5.9 m at 5 m in a room session). */
        const val ALONG_RANGE = 0.4f
        /** Motion along the ray counts only for a track whose lift sigma was within this fraction of its range (a feet ray or depth; the width prior is 20-35 %) for three quarters of the window. */
        const val ALONG_SIGMA_REL = 0.15f
        /** How far the along-ray coordinate may step back within the window and still count as one-way motion (m). */
        const val ALONG_SLACK = 0.15f
        /** The robust position is the median of the raw positions over this long, refreshed this often. */
        const val ROBUST_S = 1.0
        const val ROBUST_STEP_S = 0.5
        /** A new track this close (ground, m) to a mover, alive or gone less than GHOST_S ago, starts MOVING. */
        const val INHERIT_DIST = 1.0f
        const val GHOST_S = 2.0
        /** A track absent from the update for up to this long keeps its state (a tick without it is not a new track). */
        const val KEEP_S = 1.0
        /** A live mover without a hit for this long counts as lost (a new track beside it may be it). */
        const val LOST_S = 0.6
        /**
         * The scene counts as dragged by the pose when at least this many observed tracks' median
         * displacement is within DRAG_TOLERANCE of the camera's, which moved at least DRAG_MIN_M
         * over the window. DRAG_MIN_M sits under MOVE_DIST: at 0.5 m a slow drift (0.15 m/s) tagged
         * a chair, and would tag a person, before the correction engaged (session 16: 0.48 m).
         */
        const val DRAG_MIN_TRACKS = 2
        const val DRAG_MIN_M = 0.3f
        const val DRAG_TOLERANCE = 0.5f
        /** A vehicle is a mover only after this many hits, at this speed, on a path this straight (net / length) that never steps back more than VEHICLE_SLACK_M (see `steady`). */
        const val VEHICLE_MIN_HITS = 5
        const val VEHICLE_SPEED = 0.5f
        const val VEHICLE_STRAIGHTNESS = 0.7f
        const val VEHICLE_SLACK_M = 0.3f
        /** A vehicle's displacement over the window to be a mover: a parked car's track wandered up to 2.4 m in 3 s from a drone; a car driving at 1 m/s covers 3 m. */
        const val VEHICLE_MOVE_M = 2.0f
        /**
         * No judgement while the camera moves faster than this (m/s): a hand holding the phone
         * still does not, a holder walking does (0.3-0.6 m/s in a room session, with a seated
         * person at 2.5 m tagged while they walked), a drifting pose does.
         */
        const val EYE_MAX_SPEED = 0.5f

        /**
         * Displacement that makes a track moving: placed objects need twice a person's (their lifts
         * flip, they rarely move), carried objects in between; a vehicle VEHICLE_MOVE_M (and a
         * steady path, see `update`).
         */
        fun moveDist(cls: Int): Float = when {
            TrackedClass.vehicle(cls) -> VEHICLE_MOVE_M
            TrackedClass.placed(cls) -> 1.0f
            TrackedClass.carried(cls) -> 0.7f
            else -> MOVE_DIST
        }

        /** Component-wise median of the positions (the upper middle one for an even count). */
        fun median(samples: Collection<Pair<Double, Vec3>>): Vec3 = medianOf(samples.map { it.second })

        fun medianOf(points: List<Vec3>): Vec3 {
            val n = points.size
            if (n == 0) return Vec3.ZERO
            val xs = FloatArray(n); val ys = FloatArray(n); val zs = FloatArray(n)
            for ((i, p) in points.withIndex()) { xs[i] = p.x; ys[i] = p.y; zs[i] = p.z }
            xs.sort(); ys.sort(); zs.sort()
            return Vec3(xs[n / 2], ys[n / 2], zs[n / 2])
        }
    }
}
