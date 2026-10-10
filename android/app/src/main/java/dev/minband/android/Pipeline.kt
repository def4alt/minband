package dev.minband.android

import android.content.Context
import android.graphics.BitmapFactory
import android.media.Image
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.google.ar.core.AugmentedImage
import com.google.ar.core.AugmentedImageDatabase
import com.google.ar.core.Camera
import com.google.ar.core.Config
import com.google.ar.core.Coordinates2d
import com.google.ar.core.Frame
import com.google.ar.core.Plane
import com.google.ar.core.Session
import com.google.ar.core.TrackingFailureReason
import com.google.ar.core.TrackingState
import com.google.ar.core.exceptions.NotYetAvailableException
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Orchestrates ARCore -> detector -> tracker -> core Edge -> UDP. One instance per app. Port of
 * ios/MinBand/Pipeline.swift.
 *
 * Threads
 * - GL thread (the renderer): `Session.update`, [onFrame], and everything that touches `edge`,
 *   `transport` and `groundTruth`. `start`/`stop` are called on it too (the Activity queues them
 *   on the GLSurfaceView).
 * - detect thread (single executor): YUV -> RGB, ONNX, 3D lift and `tracker.update`, at most one
 *   frame in flight (~12 Hz), so a slow model never blocks ARCore.
 * - UDP receive thread: `edge.onDatagram` (the Rust object locks internally).
 * - main: UI state only, via [Listener].
 *
 * Rates: detection ~12 Hz, `tracker.tracks(at)` -> ground truth -> `edge.tick` at 30 Hz, Pose
 * offered to the core at 2 Hz (the core sends one only when its budget-derived interval is due),
 * HUD stats at 2 Hz. Before the origin is locked the edge is ticked with no tracks (it sends only
 * Hello) and no Pose is offered.
 */
class Pipeline(private val context: Context, private val listener: Listener) {
    enum class LinkState { OFF, WAITING, UP, LOST }

    /** Snapshot for the UI, delivered on the main thread. */
    data class UiState(
        val running: Boolean = false,
        val originLocked: Boolean = false,
        val originSource: String = "none",
        val trackCount: Int = 0,
        val bytesPerSec: Int = 0,
        val wireBytesPerSec: Int = 0,
        val seq: Long = 0,
        val thetaScale: Double = 1.0,
        val fps: Double = 0.0,
        val detectHz: Double = 0.0,
        val detectorStatus: String = "loading model",
        val depthMode: String = "-",
        val status: String = "",
        val link: LinkState = LinkState.OFF,
        val deviceId: Int = 0,
        val budgetBps: Long = 0,
    )

    data class Overlay(val boxes: List<OverlayBox>, val marks: List<LiftMark>)

    interface Listener {
        fun onState(s: UiState)
        fun onOverlay(o: Overlay)
    }

    companion object {
        private const val TAG = "MinBand Pipeline"
        const val TICKS_PER_SECOND = 120.0
        const val DETECT_INTERVAL = 1.0 / 12
        const val TRACK_INTERVAL = 1.0 / 30
        const val POSE_INTERVAL = 0.5
        const val STATS_INTERVAL = 0.5
        const val WIRE_WINDOW_S = 2.0
        const val DEPTH_GIVE_UP_S = 10.0
        const val PLANE_MIN_DROP_M = 0.8f
        const val PLANE_MAX_DROP_M = 2.5f
        const val PLANE_MIN_AREA_M2 = 0.3f
        const val AUTO_ORIGIN_S = 3.0
        /** No datagram from the server for this long (after at least one) = link lost. */
        const val LINK_LOST_AFTER = 7.0
        /** Re-lock the origin on marker updates only for real corrections, not per-frame jitter. */
        const val RELOCK_TRANSLATION = 0.02f
        const val RELOCK_ANGLE = 1.0f * Math.PI.toFloat() / 180f
        /** Origin moves beyond this (or source changes) invalidate existing tracks. */
        const val RESET_TRACKS_TRANSLATION = 0.25f
        const val MARKER_NAME = "minband-marker-a"
        const val MARKER_WIDTH_M = 0.42f
        const val UDP_IP_OVERHEAD = 28
        /** Log every datagram (`adb logcat -s "MinBand Wire"`): seq, kind, acks with their missing list. */
        const val WIRE_LOG = true
        private const val WIRE_TAG = "MinBand Wire"
    }

    val origin = Origin()
    private val tracker = Tracker()
    private val main = Handler(Looper.getMainLooper())
    private val detectExecutor = Executors.newSingleThreadExecutor { r -> Thread(r, "minband.detect").apply { priority = Thread.NORM_PRIORITY + 1 } }
    private var detector: Detector? = null          // detect thread only
    @Volatile private var detectorStatus = "loading model"

    var session: Session? = null; private set
    private var depthEnabled = false

    // GL thread only
    private var transport: Transport? = null
    private var edge: EdgeBridge? = null
    private var groundTruth: GroundTruthLog? = null
    @Volatile private var isRunning = false
    private var sessionStartNs = 0L
    private var lastDetectionTime = 0.0
    private var lastTrackTime = 0.0
    private var lastPoseTime = 0.0
    private var lastStatsTime = 0.0
    private val detecting = AtomicBoolean(false)
    private var framesSinceStats = 0
    @Volatile private var detectionsSinceStats = 0
    private var sentBytesAtStats = 0L
    private var datagramsSinceStats = 0
    private var lastTrackCount = 0
    private var lastCamera: Mat4? = null
    @Volatile private var lastReceiveMs = 0L
    private var status = ""
    private val seenMarkers = HashSet<Int>()
    private val deviceId = DeviceIdentity.id(context)

    /** Latest detection result (detect thread -> GL thread), in captured-image coordinates. */
    private class DetectionResult(val dets: List<Detection>, val trackIds: List<Int?>, val lifted: List<Pair<WorldPoint, Int>>)
    @Volatile private var latestResult: DetectionResult? = null
    private var publishedResult: DetectionResult? = null

    init {
        detectExecutor.execute {
            val d = YoloOnnxDetector(context)
            detector = d
            detectorStatus = if (d.isAvailable) "model ${d.modelName}" else "no detector: ${d.lastError}"
            publishState()
        }
    }

    // Session (main thread; the Activity owns permission and ARCore install flow)

    /** Creates and configures the ARCore session. Throws ARCore's exceptions (unavailable, etc.). */
    fun createSession(): Session {
        session?.let { return it }
        val s = Session(context)
        chooseCameraConfig(s)
        val config = Config(s)
        depthEnabled = s.isDepthModeSupported(Config.DepthMode.AUTOMATIC)
        config.depthMode = if (depthEnabled) Config.DepthMode.AUTOMATIC else Config.DepthMode.DISABLED
        config.planeFindingMode = Config.PlaneFindingMode.HORIZONTAL
        config.updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
        config.focusMode = Config.FocusMode.AUTO
        config.lightEstimationMode = Config.LightEstimationMode.DISABLED
        config.augmentedImageDatabase = markerDatabase(s)
        s.configure(config)
        session = s
        publishState()
        return s
    }

    /**
     * The camera background is drawn aspect-fill, so a 4:3 stream on a 20:9 screen shows only
     * ~60 % of its width and looks zoomed. Prefer the widest GPU texture (16:9) while keeping the
     * CPU image around 480 px tall (detector cost, and the two streams share a field of view on
     * most phones). Logs every option so a device can be tuned from logcat.
     */
    private fun chooseCameraConfig(s: Session) {
        try {
            val filter = com.google.ar.core.CameraConfigFilter(s)
                .setFacingDirection(com.google.ar.core.CameraConfig.FacingDirection.BACK)
                .setTargetFps(java.util.EnumSet.of(com.google.ar.core.CameraConfig.TargetFps.TARGET_FPS_30))
            val configs = s.getSupportedCameraConfigs(filter)
            if (configs.isEmpty()) return
            fun score(c: com.google.ar.core.CameraConfig): Double {
                val t = c.textureSize; val i = c.imageSize
                val aspect = t.width.toDouble() / t.height
                return aspect * 10 - kotlin.math.abs(i.height - 480) / 100.0
            }
            for (c in configs) Log.i(TAG, "camera config: cpu ${c.imageSize.width}x${c.imageSize.height} gpu ${c.textureSize.width}x${c.textureSize.height} fps ${c.fpsRange} score ${"%.2f".format(score(c))}")
            val best = configs.maxByOrNull { score(it) } ?: return
            s.cameraConfig = best
            Log.i(TAG, "camera config chosen: cpu ${best.imageSize.width}x${best.imageSize.height} gpu ${best.textureSize.width}x${best.textureSize.height}")
        } catch (e: Exception) {
            Log.w(TAG, "camera config: $e")
        }
    }

    private fun markerDatabase(s: Session): AugmentedImageDatabase {
        val db = AugmentedImageDatabase(s)
        try {
            val opts = BitmapFactory.Options().apply { inSampleSize = 4 }   // 4961x3473 -> ~1240x868
            val bmp = context.assets.open("minband-marker-a3.png").use { BitmapFactory.decodeStream(it, null, opts) }
            if (bmp != null) db.addImage(MARKER_NAME, bmp, MARKER_WIDTH_M) else status = "marker asset unreadable"
        } catch (e: Exception) {
            status = "no marker asset: ${e.message}"
            Log.e(TAG, "marker", e)
        }
        return db
    }

    fun resume() { session?.resume() }
    fun pause() { session?.pause() }

    // Control (GL thread)

    fun start(host: String) {
        val (h, port) = UdpTransport.parse(host)
        origin.reset()
        tracker.reset()
        seenMarkers.clear()
        sessionStartNs = 0
        lastDetectionTime = 0.0; lastTrackTime = 0.0; lastPoseTime = 0.0; lastStatsTime = 0.0
        framesSinceStats = 0; detectionsSinceStats = 0; datagramsSinceStats = 0; sentBytesAtStats = 0
        lastReceiveMs = 0
        latestResult = null; publishedResult = null
        wireSamples.clear(); wireBytesTotal = 0
        depthMissingSince = -1.0
        try {
            transport = UdpTransport(h, port) { bytes ->
                lastReceiveMs = System.currentTimeMillis()
                if (WIRE_LOG) Log.i(WIRE_TAG, "down ${bytes.size} B ${dev.minband.core.describe(bytes)}")
                edge?.onDatagram(bytes)
            }
        } catch (e: Exception) {
            status = "host: ${e.message}"
            publishState()
            return
        }
        edge = EdgeBridge(deviceId, 1 + java.util.Random().nextInt(Int.MAX_VALUE - 1))
        groundTruth = GroundTruthLog(GroundTruthLog.directory(context))
        status = ""
        isRunning = true
        publishState()
        publishOverlay(Overlay(emptyList(), emptyList()))
    }

    fun stop() {
        isRunning = false
        transport?.close(); groundTruth?.close()
        transport = null; edge = null; groundTruth = null
        lastTrackCount = 0
        latestResult = null; publishedResult = null
        publishState()
        publishOverlay(Overlay(emptyList(), emptyList()))
    }

    /** "origin: set here" fallback: camera position dropped to the floor plane, Y up. */
    fun setOriginHere() {
        val cam = lastCamera ?: return
        origin.lockManual(cam)
        tracker.reset()
        publishState()
    }

    /** Writes buffered ground-truth rows so the file can be shared. */
    fun flushLog() { groundTruth?.flush() }

    val running: Boolean get() = isRunning

    fun shutdown() {
        detectExecutor.shutdownNow()
        session?.close(); session = null
    }

    // Per frame (GL thread), after Session.update()

    fun onFrame(frame: Frame) {
        val camera = frame.camera
        val m = FloatArray(16); camera.pose.toMatrix(m, 0)
        val cameraToWorld = Mat4(m)
        lastCamera = cameraToWorld
        updateTrackingStatus(camera)
        handleTrackables(frame, cameraToWorld)

        val edge = this.edge
        if (!isRunning || edge == null) return
        val tsNs = frame.timestamp
        if (sessionStartNs == 0L) { sessionStartNs = tsNs; lastStatsTime = 0.0 }
        val ts = (tsNs - sessionStartNs) / 1e9
        val tick = (ts * TICKS_PER_SECOND).toLong().coerceAtLeast(0)
        framesSinceStats += 1

        // No marker seen within AUTO_ORIGIN_S of START while tracking is good: set the origin
        // here, as ORIGIN HERE would. Seeing the marker later replaces it. Without this a run
        // that forgets the button produces no tracks at all, which cost a demo rehearsal.
        if (!origin.isLocked && ts >= AUTO_ORIGIN_S && camera.trackingState == TrackingState.TRACKING) {
            origin.lockManual(cameraToWorld)
            tracker.reset()
            Log.i(TAG, "origin set automatically after ${AUTO_ORIGIN_S.toInt()} s without a marker")
            publishState()
        }
        val locked = origin.isLocked

        // Detection at ~12 Hz on the detect thread, never more than one frame in flight.
        if (!detecting.get() && ts - lastDetectionTime >= DETECT_INTERVAL - 0.004) {
            if (runDetection(frame, camera, cameraToWorld, locked, ts)) lastDetectionTime = ts
        }

        // Tracks -> ground truth -> core at 30 Hz.
        if (ts - lastTrackTime >= TRACK_INTERVAL - 0.004) {
            lastTrackTime = ts
            val tracks = if (locked) tracker.tracks(ts) else emptyList()
            if (locked) groundTruth?.append(tick, tracks)
            for (d in edge.tick(tracks, tick)) send(d)
            lastTrackCount = tracks.size
        }

        // Pose offered at 2 Hz once the origin means something; the core returns an empty array
        // unless a Pose is due (its interval follows the budget), and `send` drops that.
        if (locked && ts - lastPoseTime >= POSE_INTERVAL) {
            lastPoseTime = ts
            send(edge.pose(cameraToWorld, origin, tick))
        }

        // Overlay: map the newest detection result into view coordinates.
        val result = latestResult
        if (result != null && result !== publishedResult) {
            publishedResult = result
            publishOverlay(overlay(result, frame, camera))
        }

        if (ts - lastStatsTime >= STATS_INTERVAL) publishStats(ts, edge)
    }

    private fun send(d: ByteArray) {
        if (d.isEmpty()) return
        val t = transport ?: return
        t.send(d)
        datagramsSinceStats += 1
        if (WIRE_LOG) Log.i(WIRE_TAG, "up ${d.size} B ${dev.minband.core.describe(d)}")
    }

    private fun updateTrackingStatus(camera: Camera) {
        val s = when (camera.trackingState) {
            TrackingState.TRACKING -> ""
            TrackingState.STOPPED -> "tracking stopped"
            else -> when (camera.trackingFailureReason) {
                TrackingFailureReason.NONE -> "initializing, move the phone slowly"
                TrackingFailureReason.BAD_STATE -> "tracking bad state"
                TrackingFailureReason.INSUFFICIENT_LIGHT -> "too dark"
                TrackingFailureReason.EXCESSIVE_MOTION -> "slow down"
                TrackingFailureReason.INSUFFICIENT_FEATURES -> "not enough texture"
                TrackingFailureReason.CAMERA_UNAVAILABLE -> "camera unavailable"
                else -> "tracking limited"
            }
        }
        if (s != status) { status = s; publishState() }
    }

    private fun handleTrackables(frame: Frame, cam: Mat4) {
        for (img in frame.getUpdatedTrackables(AugmentedImage::class.java)) {
            if (img.trackingState != TrackingState.TRACKING) continue
            val isTracked = img.trackingMethod == AugmentedImage.TrackingMethod.FULL_TRACKING
            val added = seenMarkers.add(img.index)
            val m = FloatArray(16); img.centerPose.toMatrix(m, 0)
            val markerTransform = Mat4(m)
            var shouldLock = added || origin.source != Origin.Source.MARKER
            if (!shouldLock && isTracked) {
                val d = origin.difference(markerTransform)
                if (d != null) shouldLock = d.first > RELOCK_TRANSLATION || d.second > RELOCK_ANGLE
            }
            if (!shouldLock || !(added || isTracked)) continue
            val change = origin.lock(markerTransform)
            if (!change.wasLocked || change.previousSource != Origin.Source.MARKER || change.translation > RESET_TRACKS_TRANSLATION) {
                tracker.reset()
            }
            publishState()
        }
        for (plane in frame.getUpdatedTrackables(Plane::class.java)) {
            if (plane.type != Plane.Type.HORIZONTAL_UPWARD_FACING || plane.trackingState != TrackingState.TRACKING || plane.subsumedBy != null) continue
            val y = plane.centerPose.ty()
            // ARCore does not classify planes, so: upward-facing, at least 0.8 m below the camera
            // (skip tables), not more than 2.5 m below it (a handheld phone is never higher above
            // the floor; a measured run put a phantom plane 4 m down and sank every track), and of
            // some size (noise planes are tiny).
            val drop = cam.origin.y - y
            if (drop < PLANE_MIN_DROP_M || drop > PLANE_MAX_DROP_M) continue
            if (plane.extentX * plane.extentZ < PLANE_MIN_AREA_M2) continue
            origin.observeHorizontalPlane(y, isFloor = false)
        }
    }

    /** Returns false if no camera image was available yet. */
    private fun runDetection(frame: Frame, camera: Camera, cameraToWorld: Mat4, locked: Boolean, time: Double): Boolean {
        val image: Image = try { frame.acquireCameraImage() } catch (_: NotYetAvailableException) { return false } catch (e: Exception) { Log.w(TAG, "camera image: $e"); return false }
        val depth: DepthSampler? = if (depthEnabled && locked) {
            try { frame.acquireDepthImage16Bits().use { DepthSampler.from(it) } } catch (_: NotYetAvailableException) { null } catch (e: Exception) { null }
        } else null
        if (depthEnabled && locked) watchDepth(depth != null, time)
        val k = camera.imageIntrinsics
        val f = k.focalLength; val c = k.principalPoint; val dim = k.imageDimensions
        val intr = Lift3D.Intrinsics(f[0], f[1], c[0], c[1], dim[0], dim[1])
        // Floor for the no-depth path: the lowest detected plane, else the origin's own plane (the
        // marker lies on the floor; ORIGIN HERE drops the origin onto the floor or 1.4 m below the
        // camera), so people can be placed before ARCore has found a plane.
        val floor = origin.floorY ?: if (locked) origin.markerTransform.origin.y else null
        val geo = Lift3D.FrameGeometry(cameraToWorld, intr, floor)
        val rotation = imageRotation(frame)
        detecting.set(true)
        detectExecutor.execute {
            try {
                val dets = detector?.detect(image, rotation) ?: emptyList()
                image.close()
                val trackIds = arrayOfNulls<Int>(dets.size)
                val lifted = ArrayList<Pair<WorldPoint, Int>>()
                if (locked) {
                    val points = Lift3D.lift(dets, geo, depth, origin)
                    val ids = tracker.update(points, time)
                    for ((p, id) in points.zip(ids)) {
                        if (p.detectionIndex in dets.indices) trackIds[p.detectionIndex] = id
                        if (id != null) lifted.add(Pair(p, id))
                    }
                }
                latestResult = DetectionResult(dets, trackIds.toList(), lifted)
                detectionsSinceStats += 1
            } catch (e: Throwable) {
                Log.e(TAG, "detection", e)
                try { image.close() } catch (_: Exception) {}
            } finally {
                detecting.set(false)
            }
        }
        return true
    }

    private var depthSeen = false
    private var depthMissingSince = -1.0

    /**
     * Some phones advertise the Depth API but never deliver a depth image (ARCore's depth-from-
     * motion logs an internal rectifier error ten times a second on the Infinix X6880). After
     * [DEPTH_GIVE_UP_S] without one, switch the session to planes only so ARCore stops burning
     * CPU on it; the lift then uses the floor plane.
     */
    private fun watchDepth(got: Boolean, time: Double) {
        if (got) { depthSeen = true; return }
        if (depthSeen) return
        if (depthMissingSince < 0) { depthMissingSince = time; return }
        if (time - depthMissingSince < DEPTH_GIVE_UP_S) return
        val s = session ?: return
        try {
            val c = s.config
            c.depthMode = Config.DepthMode.DISABLED
            s.configure(c)
            depthEnabled = false
            Log.i(TAG, "no depth image in ${DEPTH_GIVE_UP_S.toInt()} s: depth disabled, floor plane only")
            publishState()
        } catch (e: Exception) {
            Log.w(TAG, "depth off: $e")
            depthSeen = true   // do not retry every frame
        }
    }

    /** Clockwise rotation (degrees) that makes the CPU image upright on this display. */
    private fun imageRotation(frame: Frame): Int {
        val inPts = floatArrayOf(0.5f, 0.5f, 1f, 0.5f)
        val out = FloatArray(4)
        frame.transformCoordinates2d(Coordinates2d.IMAGE_NORMALIZED, inPts, Coordinates2d.VIEW_NORMALIZED, out)
        val dx = out[2] - out[0]; val dy = out[3] - out[1]
        return if (absf(dx) >= absf(dy)) (if (dx >= 0) 0 else 180) else (if (dy > 0) 90 else 270)
    }

    /** Captured-image boxes -> normalized view rects; lifted points -> normalized view points. */
    private fun overlay(r: DetectionResult, frame: Frame, camera: Camera): Overlay {
        val boxes = ArrayList<OverlayBox>(r.dets.size)
        val pts = FloatArray(8); val out = FloatArray(8)
        for ((i, d) in r.dets.withIndex()) {
            val b = d.bbox
            pts[0] = b.x; pts[1] = b.y; pts[2] = b.maxX; pts[3] = b.y
            pts[4] = b.x; pts[5] = b.maxY; pts[6] = b.maxX; pts[7] = b.maxY
            frame.transformCoordinates2d(Coordinates2d.IMAGE_NORMALIZED, pts, Coordinates2d.VIEW_NORMALIZED, out)
            val xs = floatArrayOf(out[0], out[2], out[4], out[6]); val ys = floatArrayOf(out[1], out[3], out[5], out[7])
            val x0 = xs.min(); val x1 = xs.max(); val y0 = ys.min(); val y1 = ys.max()
            boxes.add(OverlayBox(i, RectF(x0, y0, x1 - x0, y1 - y0), d.classId, d.conf, r.trackIds.getOrNull(i)))
        }
        val marks = ArrayList<LiftMark>(r.lifted.size)
        if (r.lifted.isNotEmpty()) {
            val view = FloatArray(16); val proj = FloatArray(16)
            camera.getViewMatrix(view, 0); camera.getProjectionMatrix(proj, 0, 0.1f, 100f)
            val vp = Mat4(proj) * Mat4(view)
            val markerToWorld = origin.markerTransform
            for ((p, id) in r.lifted) {
                val w = markerToWorld.transformPoint(p.pos)
                val v = vp.v
                val cx = v[0] * w.x + v[4] * w.y + v[8] * w.z + v[12]
                val cy = v[1] * w.x + v[5] * w.y + v[9] * w.z + v[13]
                val cw = v[3] * w.x + v[7] * w.y + v[11] * w.z + v[15]
                if (cw <= 1e-6f) continue
                marks.add(LiftMark(p.detectionIndex, id, (cx / cw + 1f) / 2f, (1f - cy / cw) / 2f))
            }
        }
        return Overlay(boxes, marks)
    }

    /** (time, wire bytes so far) at each stats tick; the wire rate spans ~2 s like the core's estimate, so a keyframe every 2 s does not read as 0 three times out of four. */
    private val wireSamples = ArrayDeque<Pair<Double, Long>>()
    private var wireBytesTotal = 0L

    private fun publishStats(now: Double, edge: EdgeBridge) {
        val dt = maxOf(1e-3, now - lastStatsTime)
        val s = edge.stats()
        val sent = transport?.sentBytes ?: 0L
        wireBytesTotal += sent - sentBytesAtStats + UDP_IP_OVERHEAD.toLong() * datagramsSinceStats
        wireSamples.addLast(Pair(now, wireBytesTotal))
        while (wireSamples.size > 2 && now - wireSamples[1].first >= WIRE_WINDOW_S) wireSamples.removeFirst()
        val oldest = wireSamples.first()
        val span = maxOf(dt, now - oldest.first)
        val wire = Math.round((wireBytesTotal - oldest.second).toDouble() / span).toInt()
        val fps = framesSinceStats / dt
        val dhz = detectionsSinceStats / dt
        val nowMs = System.currentTimeMillis()
        val link = if (lastReceiveMs == 0L) LinkState.WAITING else if (nowMs - lastReceiveMs > LINK_LOST_AFTER * 1000) LinkState.LOST else LinkState.UP
        lastStatsTime = now; framesSinceStats = 0; detectionsSinceStats = 0
        sentBytesAtStats = sent; datagramsSinceStats = 0
        val st = UiState(
            running = true, originLocked = origin.isLocked, originSource = sourceName(), trackCount = lastTrackCount,
            bytesPerSec = s.bytesPerSecEstimate, wireBytesPerSec = wire, seq = s.seq, thetaScale = s.thetaScale,
            fps = fps, detectHz = dhz, detectorStatus = detectorStatus, depthMode = if (depthEnabled) "depth" else "planes",
            status = status, link = link, deviceId = deviceId, budgetBps = s.budgetBps,
        )
        main.post { listener.onState(st) }
    }

    private fun sourceName() = when (origin.source) { Origin.Source.NONE -> "none"; Origin.Source.MARKER -> "marker"; Origin.Source.MANUAL -> "manual" }

    private fun publishState() {
        val running = isRunning
        val st = UiState(
            running = running, originLocked = origin.isLocked, originSource = sourceName(), trackCount = lastTrackCount,
            detectorStatus = detectorStatus, depthMode = if (session == null) "-" else if (depthEnabled) "depth" else "planes",
            status = status, link = if (!running) LinkState.OFF else if (lastReceiveMs == 0L) LinkState.WAITING else LinkState.UP,
            deviceId = deviceId,
        )
        main.post { listener.onState(st) }
    }

    private fun publishOverlay(o: Overlay) { main.post { listener.onOverlay(o) } }
}
