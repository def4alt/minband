package dev.minband.android

import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * 3D nearest-neighbour association + constant-velocity Kalman per track. Birth after 3 hits,
 * death after 1 s. Velocity quality here decides how often the core sends deltas. Port of
 * ios/MinBand/Tracker.swift, same numbers.
 *
 * Pure Kotlin (no ARCore): input is `WorldPoint`s in the marker frame, output `Track`s.
 *
 * Filter: state x = [p, v] (6D). Process model is continuous white-noise acceleration with
 * spectral density `q` (m^2/s^3), measurement z = p + noise(sigma). Because F, H, Q and R are the
 * same for all three axes and the noise is isotropic, the 6x6 covariance is block-diagonal with
 * three identical 2x2 blocks, so one 2x2 covariance per track is the exact filter.
 *
 * Thread safety: `update` runs on the detection thread, `tracks(at)` on the GL thread; a lock
 * serialises them.
 */
class Tracker(val config: Config = Config()) {
    class Config(
        /** Hits needed before a track is reported (and gets an id). */
        val birthHits: Int = 3,
        /** Confirmed tracks die after this long without a hit (s). */
        val deathAfter: Double = 1.0,
        /** Tentative tracks die faster so clutter does not accumulate (s). */
        val tentativeDeathAfter: Double = 0.35,
        /** Measurement noise (m) for a point that reports none (`WorldPoint.sigma` 0); depth ~0.1 m. */
        val measurementSigma: Float = 0.1f,
        /**
         * Gating along the ray (range) and, for a hit without a ray, in every direction: the
         * per-class gate widened to this many standard deviations of the predicted position
         * plus the hit's own noise when that is larger. A track just born or long unseen does
         * not yet know its velocity, so its prediction can be off by far more than the 0.7 m a
         * settled track is allowed; a fixed gate then rejects its own next hit and a second id
         * is born beside it (the phone's slow detector did this to every walker).
         */
        val gateSigmas: Float = 3f,
        /**
         * Along the ray a hit may also differ from the prediction by this fraction of its range:
         * the ray-to-floor lift of a person whose legs are hidden (desk, chair) lands the feet
         * well behind them (1.6x the range at knee height, 2.4x at a desk), and the width prior is
         * +-30 %. Taking such a hit costs little (the robust update below barely moves a settled
         * track) while rejecting it births a ghost behind the person; so the gate reaches 2.5x the
         * range out and 0.4x in (the recovery when the feet come back into view). Across the ray
         * (bearing) the lift is good, so there the gate stays the class gate plus
         * `TrackedClass.walkSpeed` times the time since the last hit.
         */
        val alongFraction: Float = 1.5f,
        /**
         * Robust update: an innovation beyond this many standard deviations of what the filter
         * expects is weighed as if its sigma were innovation/robustSigmas, so one bad range
         * (occlusion, a merged box) nudges a settled track instead of yanking it and its velocity.
         */
        val robustSigmas: Float = 3f,
        /**
         * Two confirmed tracks of one class closer than this (on the ground) are one object seen
         * twice (the detector put a second box on a walker's torso): the one with fewer hits goes.
         * Without it the second box was born as a new id, took the walker's next hits, and the
         * first id coasted to its death with the MOVING tag; the new id started over.
         */
        val mergeDist: Float = 0.5f,
        /**
         * Velocity std of a new track, per class (m/s). The second hit's innovation is split
         * between position and velocity by this prior: at 1.5 m/s for everyone, a slow detector's
         * 0.4 s gap plus a 1 m lift jump (a cup on a table, a person behind a desk) became a
         * 2.5 m/s velocity and a two-second ghost flight. People start at a slow walk and learn
         * their speed over the next hits; carried objects are mostly set down; placed objects stay.
         */
        val initialVelocitySigma: (Int) -> Float = { cls ->
            when (cls) {
                TrackedClass.PERSON -> 0.7f
                TrackedClass.CHAIR, TrackedClass.LAPTOP, TrackedClass.TV -> 0.1f
                TrackedClass.CAR, TrackedClass.MOTORCYCLE, TrackedClass.BUS, TrackedClass.TRUCK -> 1f
                else -> 0.3f
            }
        },
        /**
         * A track unseen for longer than this stops: its position holds where the last velocity
         * carried it to and it reports zero velocity until the next hit (the covariance keeps
         * growing, so the next hit is taken and the speed relearned). A detector gap is 0.4-0.8 s
         * on the phone; a ghost that flew for the full 2 s death time at its birth velocity is not.
         */
        val coastS: Double = 1.0,
        /**
         * CWNA process noise per class (m^2/s^3). q = 0.1 gives ~0.1 m/s per-axis velocity noise at
         * 12 Hz and sigma 0.1 m (well under the core's 0.3 m/s theta_vel) and follows a 90 degree
         * turn at 1 m/s within ~0.8 s. Placed objects barely move, so a much smoother filter.
         */
        val processNoise: (Int) -> Float = { cls ->
            when (cls) {
                TrackedClass.CHAIR, TrackedClass.LAPTOP, TrackedClass.TV -> 0.02f
                TrackedClass.CAR, TrackedClass.MOTORCYCLE, TrackedClass.BUS, TrackedClass.TRUCK -> 1f
                else -> 0.1f
            }
        },
        /**
         * A hit whose box is more than this many times taller or shorter (along gravity, in
         * pixels) than the track's recent boxes is someone else, whatever the ray gates allow:
         * the box of the person a track follows changes a few percent between hits, a passer-by
         * filling the frame in front of a standing person is 2-3x taller. Without it that box,
         * lifted onto the same bearing, went through the standing person's wide along-ray gate
         * (a squat lift's sigma is metres) and jumped them 1.4 m: MOVING for a second. 0 disables.
         */
        val sizeRatio: Float = 2f,
        /**
         * ...and differs by at least this many pixels: the drone clip's people are 15-35 px tall
         * and their boxes flicker by half of that between frames (the ratio alone there split
         * 248 ids into 365); the phone's passer-by was 380 px taller.
         */
        val sizeSlackPx: Float = 40f,
        /** Detection confidence smoothing (EMA weight of a new hit). */
        val confAlpha: Float = 0.2f,
        /** Change in 0..255 units below which the reported conf does not move (stops core conf-bucket flapping). */
        val confHysteresis: Int = 12,
    ) {
        companion object {
            /**
             * The phone's configuration. The defaults assume the 12 Hz detector of the design; the
             * Android CPU detector delivers 1-4 Hz on the test phone, so a tentative track must
             * outlive the gap to its second hit or nothing is ever born (measured: boxes lifted to
             * 3D on every frame, zero tracks), and a confirmed track must survive a missed frame.
             */
            val SLOW_DETECTOR = Config(birthHits = 2, tentativeDeathAfter = 1.5, deathAfter = 2.0)
        }
    }

    private class State(var p: Vec3, var v: Vec3, var ppp: Float, var ppv: Float, var pvv: Float, var time: Double)

    private class Entry(
        val classId: Int,
        var id: Int?,
        var hits: Int,
        var lastHit: Double,
        val s: State,
        var confEMA: Float,
        var confOut: Int,
        firstSigma: Float,
        firstHeight: Float,
    ) {
        /**
         * Sigmas of the last SIGMA_WINDOW hits; the track's sigma is their median, so one poor
         * lift among good ones (a walker's box squat for a frame) does not change how its motion
         * is judged, while a seated person, all poor lifts, keeps a large one.
         */
        private val sigmas = FloatArray(SIGMA_WINDOW)
        private var sigmaN = 0
        private var sigmaAt = 0
        /** Across-ray sigmas of the last SIGMA_WINDOW hits (their median is the track's). */
        private val acrossSigmas = FloatArray(SIGMA_WINDOW)
        private var acrossN = 0
        private var acrossAt = 0
        fun pushSigmaAcross(v: Float) { acrossSigmas[acrossAt] = v; acrossAt = (acrossAt + 1) % SIGMA_WINDOW; if (acrossN < SIGMA_WINDOW) acrossN++ }
        val sigmaAcross: Float get() { if (acrossN == 0) return 0f; val a = acrossSigmas.copyOf(acrossN); a.sort(); return a[acrossN / 2] }
        /** Box heights (px along gravity) of the last SIGMA_WINDOW hits that had one; the median gates the next hit's size. */
        private val heights = FloatArray(SIGMA_WINDOW)
        private var heightN = 0
        private var heightAt = 0
        init { pushSigma(firstSigma); pushHeight(firstHeight) }
        fun pushSigma(v: Float) { sigmas[sigmaAt] = v; sigmaAt = (sigmaAt + 1) % SIGMA_WINDOW; if (sigmaN < SIGMA_WINDOW) sigmaN++ }
        fun pushHeight(v: Float) { if (v <= 0f) return; heights[heightAt] = v; heightAt = (heightAt + 1) % SIGMA_WINDOW; if (heightN < SIGMA_WINDOW) heightN++ }
        val sigma: Float get() { val a = sigmas.copyOf(sigmaN); a.sort(); return a[sigmaN / 2] }
        /** Median box height of the recent hits, 0 when none reported one. */
        val heightPx: Float get() { if (heightN == 0) return 0f; val a = heights.copyOf(heightN); a.sort(); return a[heightN / 2] }
    }

    private val entries = ArrayList<Entry>()
    private var nextId = 1
    private val lock = Any()

    private companion object {
        /** Weight of the along-ray distance in the association cost (bearing counts for four times as much). */
        const val ALONG_COST = 0.25f
        /** A vehicle's duplicate is farther from it than a person's: the detector's second box on a 4.5 m car sits on its other half. 1.5 m, under the 2.5 m between cars parked side by side. */
        const val VEHICLE_MERGE_SCALE = 3f
        /** Hits whose sigmas make a track's sigma (their median). */
        const val SIGMA_WINDOW = 5
    }

    /** Number of confirmed tracks alive right now (any time). */
    val confirmedCount: Int get() = synchronized(lock) { entries.count { it.id != null } }

    /** Drops every track but keeps the id counter, so ids are never reused within a session. */
    fun reset() = synchronized(lock) { entries.clear() }

    /**
     * Associate `points` (marker frame, observed at `time` seconds) with tracks and run the KF
     * update. Returns, per input point, the id of the confirmed track it updated (null for
     * tentative or newly created tracks).
     */
    fun update(points: List<WorldPoint>, time: Double): List<Int?> = synchronized(lock) {
        prune(time)

        // Predict every track to `time` (only forward; a late measurement is applied at the
        // track's own time).
        for (e in entries) if (time > e.s.time) predictCoasting(e, time)

        // Greedy global nearest neighbour inside per-class gates. With the camera position known
        // the gate is split into across the ray (bearing, tight) and along it (range, loose) and
        // the cost favours bearing agreement; without it, one isotropic gate.
        class Pair3(val d: Float, val t: Int, val m: Int)
        val pairs = ArrayList<Pair3>()
        for ((m, pt) in points.withIndex()) {
            val base = TrackedClass.gate(pt.classId)
            val r = noise(pt)
            val eye = pt.eye
            val u = rayDirection(pt)
            val range = if (eye != null) Vec2(pt.pos.x - eye.x, pt.pos.z - eye.z).length else 0f
            for ((t, e) in entries.withIndex()) {
                if (!TrackedClass.sameKind(e.classId, pt.classId)) continue
                if (config.sizeRatio > 0f && pt.heightPx > 0f) {
                    val h = e.heightPx
                    if (h > 0f && absf(pt.heightPx - h) > config.sizeSlackPx && (pt.heightPx > config.sizeRatio * h || h > config.sizeRatio * pt.heightPx)) continue
                }
                val d = pt.pos - e.s.p
                val sigma = sqrt(e.s.ppp + r)
                if (u != null) {
                    // Ground projection only: y is the lift's guess at height (a box centre
                    // while the floor is unknown) and must not read as a bearing difference.
                    val along = d.x * u.x + d.z * u.z
                    val ax = d.x - u.x * along; val az = d.z - u.z * along
                    val across = sqrt(ax * ax + az * az)
                    val sinceHit = (time - e.lastHit).toFloat().coerceAtLeast(0f)
                    val acrossGate = maxOf(base, TrackedClass.walkSpeed(pt.classId) * sinceHit)
                    // The range-wide along gate is for a person whose legs are hidden; a vehicle's
                    // box bottom is its ground contact, and 1.5x its 50 m range from a drone let a
                    // parked car's track hop to the next car along the ray whenever its own box
                    // was missed for a frame.
                    val alongGate = if (TrackedClass.vehicle(pt.classId)) maxOf(config.gateSigmas * sigma, acrossGate)
                        else maxOf(config.gateSigmas * sigma, config.alongFraction * range)
                    if (across > acrossGate || absf(along) > alongGate) continue
                    pairs.add(Pair3(across + ALONG_COST * absf(along), t, m))
                } else {
                    val dist = d.length
                    if (dist > maxOf(base, config.gateSigmas * sigma)) continue
                    pairs.add(Pair3(dist, t, m))
                }
            }
        }
        pairs.sortWith(compareBy<Pair3> { it.d }.thenBy { it.t }.thenBy { it.m })
        val trackUsed = BooleanArray(entries.size)
        val pointTrack = arrayOfNulls<Int>(points.size)
        for (pr in pairs) if (!trackUsed[pr.t] && pointTrack[pr.m] == null) {
            trackUsed[pr.t] = true; pointTrack[pr.m] = pr.t
        }

        val result = arrayOfNulls<Int>(points.size)
        for ((m, pt) in points.withIndex()) {
            val t = pointTrack[m]
            if (t != null) {
                val e = entries[t]
                val r = noise(pt)
                correct(e.s, pt.pos, r, noiseAcross(pt), rayDirection(pt))
                val vmax = TrackedClass.maxSpeed(e.classId); val sp = e.s.v.length
                if (sp > vmax) e.s.v = e.s.v * (vmax / sp)       // the state too, not only the report
                e.hits += 1
                e.pushSigma(sqrt(r))
                e.pushHeight(pt.heightPx)
                e.pushSigmaAcross(pt.sigmaAcross)
                e.lastHit = maxOf(e.lastHit, time)
                updateConf(e, pt.conf)
                if (e.id == null && e.hits >= config.birthHits) { e.id = nextId; nextId += 1 }
                result[m] = e.id
            } else {
                val r = noise(pt)
                val vs = config.initialVelocitySigma(pt.classId)
                val c = clampf(pt.conf, 0f, 1f)
                val e = Entry(pt.classId, null, 1, time, State(pt.pos, Vec3.ZERO, r, 0f, vs * vs, time), c, (c * 255f).roundToInt(), sqrt(r), pt.heightPx).also { it.pushSigmaAcross(pt.sigmaAcross) }
                if (config.birthHits <= 1) { e.id = nextId; nextId += 1; result[m] = e.id }
                entries.add(e)
            }
        }
        result.toList()
    }

    /**
     * Confirmed tracks predicted to `time` (not mutating the filters). Velocity is clamped to the
     * class max speed and the clamped velocity is what the position is extrapolated with.
     */
    fun tracks(time: Double): List<Track> = synchronized(lock) {
        prune(time)
        val out = ArrayList<Track>(entries.size)
        for (e in entries) {
            val id = e.id ?: continue
            val vmax = TrackedClass.maxSpeed(e.classId)
            var v = e.s.v
            val speed = v.length
            if (speed > vmax) v = v * (vmax / speed)
            // Extrapolate only up to the coasting horizon; past it the track stands still.
            val horizon = e.lastHit + config.coastS
            val dt = (minOf(time, horizon) - e.s.time).coerceAtLeast(0.0).toFloat()
            out.add(Track(id, e.classId, e.s.p + v * dt, if (time > horizon) Vec3.ZERO else v, e.confOut, e.hits, e.sigma, e.sigmaAcross))
        }
        out.sortBy { it.id }
        out
    }

    // Internals (lock held).

    private fun prune(time: Double) {
        entries.removeAll { e ->
            val silent = time - e.lastHit
            if (e.id == null) silent > config.tentativeDeathAfter else silent > config.deathAfter
        }
        // Duplicates (see Config.mergeDist): keep the better-supported of two confirmed tracks
        // of one class on the same spot.
        if (config.mergeDist > 0f) {
            var i = 0
            while (i < entries.size) {
                val a = entries[i]
                if (a.id == null) { i++; continue }
                var removedA = false
                var j = i + 1
                while (j < entries.size) {
                    val b = entries[j]
                    if (b.id != null && TrackedClass.sameKind(b.classId, a.classId) && groundDistance(a.s.p, b.s.p) < config.mergeDist * (if (TrackedClass.vehicle(a.classId)) VEHICLE_MERGE_SCALE else 1f)) {
                        if (b.hits > a.hits) { entries.removeAt(i); removedA = true; break }
                        entries.removeAt(j); continue
                    }
                    j++
                }
                if (!removedA) i++
            }
        }
    }

    private fun groundDistance(a: Vec3, b: Vec3): Float { val dx = a.x - b.x; val dz = a.z - b.z; return sqrt(dx * dx + dz * dz) }

    /** Predict `e` to `time`; past `coastS` without a hit the position holds and the velocity is dropped, the covariance keeps growing. */
    private fun predictCoasting(e: Entry, time: Double) {
        val q = config.processNoise(e.classId)
        val horizon = e.lastHit + config.coastS
        if (time <= horizon) { predict(e.s, time, q); return }
        if (e.s.time < horizon) predict(e.s, horizon, q)
        e.s.v = Vec3.ZERO
        predict(e.s, time, q)
    }

    private fun predict(s: State, time: Double, q: Float) {
        val dt = (time - s.time).toFloat()
        if (dt <= 0f) return
        s.p = s.p + s.v * dt
        val dt2 = dt * dt; val dt3 = dt2 * dt
        val ppp = s.ppp + 2f * dt * s.ppv + dt2 * s.pvv + q * dt3 / 3f
        val ppv = s.ppv + dt * s.pvv + q * dt2 / 2f
        val pvv = s.pvv + q * dt
        s.ppp = ppp; s.ppv = ppv; s.pvv = pvv
        s.time = time
    }

    /** Measurement variance of a hit along the ray (and everywhere, without a ray): its own reported sigma, else the configured default. */
    private fun noise(pt: WorldPoint): Float {
        val s = if (pt.sigma > 0f) pt.sigma else config.measurementSigma
        return s * s
    }

    /** Measurement variance across the ray; 0 when the hit does not say (then isotropic). */
    private fun noiseAcross(pt: WorldPoint): Float = pt.sigmaAcross * pt.sigmaAcross

    /**
     * Unit direction of the ray from the camera to the hit, projected on the ground: lifted points
     * sit on the floor, so a range error moves them along the ray's footprint, not along the 3D
     * line of sight from a raised camera. Null without a camera position.
     */
    private fun rayDirection(pt: WorldPoint): Vec3? {
        val eye = pt.eye ?: return null
        val ray = Vec3(pt.pos.x - eye.x, 0f, pt.pos.z - eye.z)
        val range = ray.length
        return if (range > 1e-3f) ray * (1f / range) else null
    }

    /** Robust measurement variance: beyond robustSigmas of the expected innovation, inflated so the innovation sits exactly there (an outlier moves the state a little, not all the way; a fresh track with a large ppp still takes big innovations: it is learning). */
    private fun robust(yy: Float, ppp: Float, r: Float): Float {
        val k2 = config.robustSigmas * config.robustSigmas
        return if (yy > k2 * (ppp + r)) maxOf(r, yy / k2 - ppp) else r
    }

    /**
     * Kalman update with the hit. With a ray and a separate across-ray noise the innovation is
     * split: across the ray (bearing, good) and along it (range, often poor) each get their own
     * gain and robust weight, so a walker crossing the view moves the track even when their range
     * is guesswork, and a range flip barely does. The shared covariance follows the across part,
     * the better one; the along estimate is then held a little too confidently, which the process
     * noise undoes within a few hits.
     */
    private fun correct(s: State, z: Vec3, r: Float, rAcross: Float, u: Vec3?) {
        val y = z - s.p
        if (u == null || rAcross <= 0f || rAcross >= r) {
            val yy = y.x * y.x + y.y * y.y + y.z * y.z
            val rEff = robust(yy, s.ppp, r)
            val sInv = 1f / (s.ppp + rEff)
            val kp = s.ppp * sInv; val kv = s.ppv * sInv
            s.p = s.p + y * kp
            s.v = s.v + y * kv
            val ppp = (1f - kp) * s.ppp
            val ppv = (1f - kp) * s.ppv
            val pvv = s.pvv - kv * s.ppv
            s.ppp = ppp; s.ppv = ppv; s.pvv = maxOf(pvv, 1e-6f)
            return
        }
        // Horizontal split; the height innovation goes with the range part (a guess too while
        // the floor is unknown).
        val along = y.x * u.x + y.z * u.z
        val yAlong = Vec3(u.x * along, y.y, u.z * along)
        val yAcross = Vec3(y.x - yAlong.x, 0f, y.z - yAlong.z)
        val rA = robust(yAcross.x * yAcross.x + yAcross.y * yAcross.y + yAcross.z * yAcross.z, s.ppp, rAcross)
        val rL = robust(along * along, s.ppp, r)
        val sA = 1f / (s.ppp + rA); val sL = 1f / (s.ppp + rL)
        val kpA = s.ppp * sA; val kvA = s.ppv * sA
        val kpL = s.ppp * sL; val kvL = s.ppv * sL
        s.p = s.p + yAcross * kpA + yAlong * kpL
        s.v = s.v + yAcross * kvA + yAlong * kvL
        val ppp = (1f - kpA) * s.ppp
        val ppv = (1f - kpA) * s.ppv
        val pvv = s.pvv - kvA * s.ppv
        s.ppp = ppp; s.ppv = ppv; s.pvv = maxOf(pvv, 1e-6f)
    }

    private fun updateConf(e: Entry, conf: Float) {
        val c = clampf(conf, 0f, 1f)
        e.confEMA += config.confAlpha * (c - e.confEMA)
        val target = (e.confEMA * 255f).roundToInt()
        if (kotlin.math.abs(target - e.confOut) >= config.confHysteresis) e.confOut = target
    }
}
