package dev.minband.android

import kotlin.math.roundToInt

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
        /** Measurement noise (depth ~0.1 m). */
        val measurementSigma: Float = 0.1f,
        /** Initial velocity std for a new track. */
        val initialVelocitySigma: Float = 1.5f,
        /**
         * CWNA process noise per class (m^2/s^3). q = 0.1 gives ~0.1 m/s per-axis velocity noise at
         * 12 Hz and sigma 0.1 m (well under the core's 0.3 m/s theta_vel) and follows a 90 degree
         * turn at 1 m/s within ~0.8 s. Placed objects barely move, so a much smoother filter.
         */
        val processNoise: (Int) -> Float = { cls ->
            when (cls) {
                TrackedClass.CHAIR, TrackedClass.LAPTOP, TrackedClass.TV -> 0.02f
                else -> 0.1f
            }
        },
        /** Detection confidence smoothing (EMA weight of a new hit). */
        val confAlpha: Float = 0.2f,
        /** Change in 0..255 units below which the reported conf does not move (stops core conf-bucket flapping). */
        val confHysteresis: Int = 12,
    )

    private class State(var p: Vec3, var v: Vec3, var ppp: Float, var ppv: Float, var pvv: Float, var time: Double)

    private class Entry(
        val classId: Int,
        var id: Int?,
        var hits: Int,
        var lastHit: Double,
        val s: State,
        var confEMA: Float,
        var confOut: Int,
    )

    private val entries = ArrayList<Entry>()
    private var nextId = 1
    private val lock = Any()

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
        for (e in entries) if (time > e.s.time) predict(e.s, time, config.processNoise(e.classId))

        // Greedy global nearest neighbour inside per-class gates.
        class Pair3(val d: Float, val t: Int, val m: Int)
        val pairs = ArrayList<Pair3>()
        for ((m, pt) in points.withIndex()) {
            val gate = TrackedClass.gate(pt.classId)
            for ((t, e) in entries.withIndex()) {
                if (e.classId != pt.classId) continue
                val d = e.s.p.distance(pt.pos)
                if (d <= gate) pairs.add(Pair3(d, t, m))
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
                correct(e.s, pt.pos)
                e.hits += 1
                e.lastHit = maxOf(e.lastHit, time)
                updateConf(e, pt.conf)
                if (e.id == null && e.hits >= config.birthHits) { e.id = nextId; nextId += 1 }
                result[m] = e.id
            } else {
                val r = config.measurementSigma * config.measurementSigma
                val vs = config.initialVelocitySigma
                val c = clampf(pt.conf, 0f, 1f)
                val e = Entry(pt.classId, null, 1, time, State(pt.pos, Vec3.ZERO, r, 0f, vs * vs, time), c, (c * 255f).roundToInt())
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
            val dt = maxOf(0.0, time - e.s.time).toFloat()
            out.add(Track(id, e.classId, e.s.p + v * dt, v, e.confOut))
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

    private fun correct(s: State, z: Vec3) {
        val r = config.measurementSigma * config.measurementSigma
        val sInv = 1f / (s.ppp + r)
        val kp = s.ppp * sInv; val kv = s.ppv * sInv
        val y = z - s.p
        s.p = s.p + y * kp
        s.v = s.v + y * kv
        val ppp = (1f - kp) * s.ppp
        val ppv = (1f - kp) * s.ppv
        val pvv = s.pvv - kv * s.ppv
        s.ppp = ppp; s.ppv = ppv; s.pvv = maxOf(pvv, 1e-6f)
    }

    private fun updateConf(e: Entry, conf: Float) {
        val c = clampf(conf, 0f, 1f)
        e.confEMA += config.confAlpha * (c - e.confEMA)
        val target = (e.confEMA * 255f).roundToInt()
        if (kotlin.math.abs(target - e.confOut) >= config.confHysteresis) e.confOut = target
    }
}
