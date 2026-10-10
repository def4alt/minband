package dev.minband.android

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Matrix
import android.graphics.Paint
import android.media.Image
import android.util.Log
import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import ai.onnxruntime.TensorInfo
import java.nio.FloatBuffer

/**
 * Object detector interface: a CPU camera image (YUV_420_888, sensor orientation) and the rotation
 * that makes it upright for the user, to boxes in normalized captured-image coordinates (sensor
 * orientation, origin top-left), the space Lift3D and the depth image use.
 *
 * [YoloOnnxDetector] is the stock implementation (ONNX Runtime). A ClikaRT detector slots in here
 * with the same contract. Not thread-safe: create and use it on one thread (the detection thread).
 */
interface Detector {
    val isAvailable: Boolean
    val modelName: String?
    val lastError: String?
    fun detect(image: Image, rotationDegrees: Int): List<Detection>
}

/**
 * YOLOv8/YOLO11 (Ultralytics ONNX export without NMS, e.g. `yolov8n` at 320 px: input
 * `[1,3,S,S]` RGB 0..1, output `[1, 4 + 80, N]` with cx, cy, w, h in input pixels then 80 COCO
 * scores). Model: the first `.onnx` in the app's assets. Preprocessing mirrors iOS (scaleFill: the
 * upright image is stretched to the square input, no letterbox). NMS per class at IoU 0.45, score
 * >= 0.35, only the tracked COCO classes are kept.
 */
class YoloOnnxDetector(context: Context) : Detector {
    companion object {
        const val CONFIDENCE_THRESHOLD = 0.35f
        const val IOU_THRESHOLD = 0.45f
        private const val TAG = "MinBand Detector"
        private const val MAX_DETECTIONS = 50

        /**
         * Box in the *upright* image (normalized, origin top-left) -> normalized rect in the raw
         * captured image (sensor orientation, origin top-left). `rotationDegrees` is the clockwise
         * rotation that made the raw image upright. Same mapping as iOS `capturedImageRect`.
         */
        fun capturedImageRect(b: RectF, rotationDegrees: Int): RectF {
            val ox = b.x; val oy = b.y; val ow = b.width; val oh = b.height
            return when (((rotationDegrees % 360) + 360) % 360) {
                90 -> RectF(oy, 1f - ox - ow, oh, ow)          // displayed = raw rotated 90 deg clockwise (portrait)
                270 -> RectF(1f - oy - oh, ox, oh, ow)         // 90 deg counter-clockwise
                180 -> RectF(1f - ox - ow, 1f - oy - oh, ow, oh)
                else -> RectF(ox, oy, ow, oh)                  // landscape, no rotation
            }
        }
    }

    override var isAvailable = false; private set
    override var modelName: String? = null; private set
    override var lastError: String? = null; private set

    private var env: OrtEnvironment? = null
    private var session: OrtSession? = null
    private var inputName = "images"
    private var inputSize = 320
    private var rgb: Bitmap? = null                 // image-sized, sensor orientation
    private var square: Bitmap? = null              // inputSize x inputSize, upright
    private var pixels = IntArray(0)
    private var floats = FloatArray(0)
    private val paint = Paint(Paint.FILTER_BITMAP_FLAG)

    init {
        val assets = context.assets
        val names = try { assets.list("")?.filter { it.endsWith(".onnx") }?.sorted() ?: emptyList() } catch (e: Exception) { emptyList() }
        if (names.isEmpty()) {
            lastError = "no .onnx model in assets (see android/README.md)"
        } else {
            for (name in names) {
                try {
                    val bytes = assets.open(name).use { it.readBytes() }
                    val e = OrtEnvironment.getEnvironment()
                    // CPU (XNNPACK-free default kernels, 4 threads): predictable 20-60 ms for
                    // yolov8n at 320 on a 2024 mid-range SoC. NNAPI partitions YOLO badly and is
                    // often slower; add it here if a measured run on the demo phone says otherwise.
                    val opts = OrtSession.SessionOptions().apply {
                        setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
                        // XNNPACK runs the convolutions on its own 4-thread pool (ORT's intra-op
                        // threads then stay at 1, as its docs ask); on a 2024 mid-range SoC it
                        // roughly halves yolov8n-320 against the default CPU kernels.
                        try {
                            addXnnpack(mapOf("intra_op_num_threads" to "4"))
                            setIntraOpNumThreads(1)
                        } catch (_: Throwable) {
                            setIntraOpNumThreads(4)
                        }
                    }
                    val s = e.createSession(bytes, opts)
                    inputName = s.inputNames.first()
                    val info = s.inputInfo[inputName]?.info as? TensorInfo
                    val shape = info?.shape
                    if (shape != null && shape.size == 4 && shape[2] > 0) inputSize = shape[2].toInt()
                    env = e; session = s
                    modelName = name.removeSuffix(".onnx")
                    isAvailable = true
                    break
                } catch (e: Throwable) {
                    lastError = "$name: ${e.message}"
                    Log.e(TAG, "failed to load $name", e)
                }
            }
        }
        square = Bitmap.createBitmap(inputSize, inputSize, Bitmap.Config.ARGB_8888)
        pixels = IntArray(inputSize * inputSize)
        floats = FloatArray(3 * inputSize * inputSize)
    }

    override fun detect(image: Image, rotationDegrees: Int): List<Detection> {
        val s = session ?: return emptyList()
        val rot = ((rotationDegrees % 360) + 360) % 360
        try {
            val t0 = android.os.SystemClock.elapsedRealtimeNanos()
            val src = toRgb(image)
            val t1 = android.os.SystemClock.elapsedRealtimeNanos()
            val sq = square!!
            // Upright image letterboxed into the square input (aspect kept, grey padding), the
            // preprocessing YOLO was trained with. Stretching a 4:3 or 16:9 frame to a square
            // (what the first version did) makes people tall and thin and costs recall up close.
            val upW = if (rot == 90 || rot == 270) src.height else src.width
            val upH = if (rot == 90 || rot == 270) src.width else src.height
            val scale = minOf(inputSize / upW.toFloat(), inputSize / upH.toFloat())
            letterScale = scale
            letterW = upW * scale; letterH = upH * scale
            letterDx = (inputSize - letterW) / 2f; letterDy = (inputSize - letterH) / 2f
            val m = Matrix()
            m.postTranslate(-src.width / 2f, -src.height / 2f)
            m.postRotate(rot.toFloat())
            m.postScale(scale, scale)
            m.postTranslate(inputSize / 2f, inputSize / 2f)
            val canvas = Canvas(sq)
            canvas.drawColor(0xFF727272.toInt())
            canvas.drawBitmap(src, m, paint)
            sq.getPixels(pixels, 0, inputSize, 0, 0, inputSize, inputSize)
            val n = inputSize * inputSize
            for (i in 0 until n) {
                val p = pixels[i]
                floats[i] = ((p shr 16) and 0xFF) / 255f
                floats[n + i] = ((p shr 8) and 0xFF) / 255f
                floats[2 * n + i] = (p and 0xFF) / 255f
            }
            val t2 = android.os.SystemClock.elapsedRealtimeNanos()
            val shape = longArrayOf(1, 3, inputSize.toLong(), inputSize.toLong())
            OnnxTensor.createTensor(env, FloatBuffer.wrap(floats), shape).use { input ->
                s.run(mapOf(inputName to input)).use { result ->
                    val t3 = android.os.SystemClock.elapsedRealtimeNanos()
                    @Suppress("UNCHECKED_CAST")
                    val out = result[0].value as Array<Array<FloatArray>>
                    val dets = decode(out[0], rot)
                    val t4 = android.os.SystemClock.elapsedRealtimeNanos()
                    if (++frames % 24 == 0) Log.i(TAG, "yuv ${(t1 - t0) / 1_000_000} ms, prep ${(t2 - t1) / 1_000_000} ms, run ${(t3 - t2) / 1_000_000} ms, decode ${(t4 - t3) / 1_000_000} ms, ${dets.size} boxes")
                    return dets
                }
            }
        } catch (e: Throwable) {
            lastError = e.message
            Log.e(TAG, "detect failed", e)
            return emptyList()
        }
    }

    /** `rows`: [4 + classes][anchors]. Boxes come back in captured-image (sensor) coordinates. */
    private fun decode(rows: Array<FloatArray>, rotationDegrees: Int): List<Detection> {
        val anchors = rows[0].size
        val classes = rows.size - 4
        class Cand(val cls: Int, val score: Float, val x: Float, val y: Float, val w: Float, val h: Float)
        val cands = ArrayList<Cand>()
        for (j in 0 until anchors) {
            var best = -1; var bestScore = 0f
            for (c in 0 until classes) {
                val sc = rows[4 + c][j]
                if (sc > bestScore) { bestScore = sc; best = c }
            }
            if (best < 0 || bestScore < CONFIDENCE_THRESHOLD || best !in TrackedClass.all) continue
            // Input pixels -> normalized upright image, undoing the letterbox; clipped to the image.
            val x0 = clampf((rows[0][j] - rows[2][j] / 2f - letterDx) / letterW, 0f, 1f)
            val y0 = clampf((rows[1][j] - rows[3][j] / 2f - letterDy) / letterH, 0f, 1f)
            val x1 = clampf((rows[0][j] + rows[2][j] / 2f - letterDx) / letterW, 0f, 1f)
            val y1 = clampf((rows[1][j] + rows[3][j] / 2f - letterDy) / letterH, 0f, 1f)
            if (x1 - x0 < 1e-3f || y1 - y0 < 1e-3f) continue
            cands.add(Cand(best, bestScore, x0, y0, x1 - x0, y1 - y0))
        }
        cands.sortByDescending { it.score }
        val kept = ArrayList<Cand>()
        for (c in cands) {
            if (kept.size >= MAX_DETECTIONS) break
            var suppressed = false
            for (k in kept) if (k.cls == c.cls && iou(k.x, k.y, k.w, k.h, c.x, c.y, c.w, c.h) > IOU_THRESHOLD) { suppressed = true; break }
            if (!suppressed) kept.add(c)
        }
        return kept.map { Detection(it.cls, capturedImageRect(RectF(it.x, it.y, it.w, it.h), rotationDegrees), it.score) }
    }

    private fun iou(ax: Float, ay: Float, aw: Float, ah: Float, bx: Float, by: Float, bw: Float, bh: Float): Float {
        val ix = maxOf(0f, minOf(ax + aw, bx + bw) - maxOf(ax, bx))
        val iy = maxOf(0f, minOf(ay + ah, by + bh) - maxOf(ay, by))
        val inter = ix * iy
        val union = aw * ah + bw * bh - inter
        return if (union <= 0f) 0f else inter / union
    }

    /** YUV_420_888 -> ARGB bitmap at the image's own size (sensor orientation). Reused buffers. */
    private fun toRgb(image: Image): Bitmap {
        val w = image.width; val h = image.height
        var bmp = rgb
        if (bmp == null || bmp.width != w || bmp.height != h) {
            bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888); rgb = bmp
            if (rgbPixels.size != w * h) rgbPixels = IntArray(w * h)
        }
        YuvToRgb.convert(image, rgbPixels)
        bmp.setPixels(rgbPixels, 0, w, 0, 0, w, h)
        return bmp
    }
    private var rgbPixels = IntArray(0)
    private var frames = 0
    // Letterbox of the last frame: scale and the padded image's size and offset inside the input.
    private var letterScale = 1f
    private var letterW = 1f
    private var letterH = 1f
    private var letterDx = 0f
    private var letterDy = 0f

    fun close() {
        session?.close(); session = null
    }
}

/**
 * YUV_420_888 (any plane strides) -> packed ARGB ints. BT.601 full range, like the camera HAL's
 * JPEG path. The planes are bulk-copied into byte arrays first: per-pixel `ByteBuffer.get` is
 * what made the first version cost ~60 ms per 640x480 frame.
 */
object YuvToRgb {
    private var yBytes = ByteArray(0)
    private var uBytes = ByteArray(0)
    private var vBytes = ByteArray(0)

    private fun copy(buf: java.nio.ByteBuffer, into: ByteArray): ByteArray {
        val n = buf.remaining()
        val arr = if (into.size >= n) into else ByteArray(n)
        buf.position(0)
        buf.get(arr, 0, n)
        return arr
    }

    fun convert(image: Image, out: IntArray) {
        val w = image.width; val h = image.height
        val yP = image.planes[0]; val uP = image.planes[1]; val vP = image.planes[2]
        yBytes = copy(yP.buffer, yBytes); uBytes = copy(uP.buffer, uBytes); vBytes = copy(vP.buffer, vBytes)
        val ya = yBytes; val ua = uBytes; val va = vBytes
        val yRow = yP.rowStride; val yPix = yP.pixelStride
        val uRow = uP.rowStride; val uPix = uP.pixelStride
        val vRow = vP.rowStride; val vPix = vP.pixelStride
        var o = 0
        for (row in 0 until h) {
            val yBase = row * yRow
            val cRowU = (row shr 1) * uRow
            val cRowV = (row shr 1) * vRow
            var col = 0
            while (col < w) {
                val yv = ya[yBase + col * yPix].toInt() and 0xFF
                val ci = col shr 1
                val u = (ua[cRowU + ci * uPix].toInt() and 0xFF) - 128
                val v = (va[cRowV + ci * vPix].toInt() and 0xFF) - 128
                // Fixed point BT.601: R = Y + 1.402 V, G = Y - 0.344 U - 0.714 V, B = Y + 1.772 U
                val y1192 = 1192 * yv
                var r = (y1192 + 1634 * v) shr 10
                var g = (y1192 - 400 * u - 833 * v) shr 10
                var b = (y1192 + 2066 * u) shr 10
                if (r < 0) r = 0 else if (r > 255) r = 255
                if (g < 0) g = 0 else if (g > 255) g = 255
                if (b < 0) b = 0 else if (b > 255) b = 255
                out[o++] = (0xFF shl 24) or (r shl 16) or (g shl 8) or b
                col++
            }
        }
    }
}
