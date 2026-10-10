package dev.minband.android

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Matrix
import android.graphics.Paint
import android.media.Image
import android.util.Log
import org.tensorflow.lite.Interpreter
import org.tensorflow.lite.gpu.GpuDelegate
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.channels.FileChannel

/**
 * YOLOv8/YOLO11 on TFLite with the **GPU delegate** (OpenCL/OpenGL on the phone's GPU), the one
 * Android path that actually offloads the convolutions off the CPU. The model is an Ultralytics /
 * onnx2tf export without NMS: input `[1,S,S,3]` RGB 0..1 (NHWC), output `[1, 4+80, N]` or its
 * transpose `[1, N, 4+80]` with cx, cy, w, h in input pixels then 80 COCO scores. NMS per class and
 * the class filter run here, exactly as in [YoloOnnxDetector]; the letterbox, class ids, confidence
 * and IoU thresholds, and the captured-image mapping are shared with it.
 *
 * Falls back to the TFLite CPU (XNNPACK) when the GPU delegate is not supported, so the detector is
 * still available; if there is no `.tflite` in assets at all, [isAvailable] is false and the caller
 * uses the ONNX detector instead. Not thread-safe: one detection thread.
 */
class TfliteDetector(context: Context) : Detector {
    companion object {
        private const val TAG = "MinBand Detector"
    }

    override var isAvailable = false; private set
    override var modelName: String? = null; private set
    override var lastError: String? = null; private set
    /** "tflite-gpu" when the GPU delegate took it, else "tflite-cpu". */
    var provider: String = "tflite-cpu"; private set

    private var interpreter: Interpreter? = null
    private var gpuDelegate: GpuDelegate? = null
    private var inputSize = 256
    private var outClassesFirst = true   // output [1, 84, N] (true) vs [1, N, 84] (false)
    private var outDim1 = 84
    private var outDim2 = 1344

    private var square: Bitmap? = null
    private var pixels = IntArray(0)
    private var inputBuf: ByteBuffer? = null
    private var rgb: Bitmap? = null
    private var rgbPixels = IntArray(0)
    private val paint = Paint(Paint.FILTER_BITMAP_FLAG)
    private var frames = 0

    // Letterbox of the last frame (same meaning as YoloOnnxDetector).
    private var letterW = 1f
    private var letterH = 1f
    private var letterDx = 0f
    private var letterDy = 0f

    init {
        val assets = context.assets
        val name = try { assets.list("")?.firstOrNull { it.endsWith(".tflite") } } catch (_: Exception) { null }
        if (name == null) {
            lastError = "no .tflite model in assets"
        } else {
            try {
                val model = assets.openFd(name).use { fd ->
                    FileInputStream(fd.fileDescriptor).channel.map(FileChannel.MapMode.READ_ONLY, fd.startOffset, fd.declaredLength)
                }
                // Try the OpenCL/OpenGL GPU delegate first. Use a plain GpuDelegate with precision
                // loss allowed (FP16 on the GPU), not CompatibilityList.bestOptionsForThisDevice:
                // on this MediaTek SoC that routes to the Neuron NPU, which fails to take the yolov8
                // graph ("Error applying delegate"). The GPU delegate partitions what it supports and
                // leaves the rest on the CPU. If applying it still throws, fall back to TFLite CPU
                // (XNNPACK) so the detector stays alive.
                val interp: Interpreter = try {
                    @Suppress("DEPRECATION")
                    val d = GpuDelegate(GpuDelegate.Options().setPrecisionLossAllowed(true))
                    val gpuOpts = Interpreter.Options().addDelegate(d)
                    val i = Interpreter(model, gpuOpts)
                    gpuDelegate = d
                    provider = "tflite-gpu"
                    i
                } catch (t: Throwable) {
                    Log.w(TAG, "GPU delegate not applied, TFLite CPU: ${t.message?.take(120)}")
                    gpuDelegate?.close(); gpuDelegate = null
                    val cpuOpts = Interpreter.Options().setNumThreads(4).apply { setUseXNNPACK(true) }
                    provider = "tflite-cpu"
                    Interpreter(model, cpuOpts)
                }
                val inShape = interp.getInputTensor(0).shape()   // [1, H, W, 3]
                if (inShape.size == 4) inputSize = inShape[1]
                val outShape = interp.getOutputTensor(0).shape()  // [1, 84, N] or [1, N, 84]
                outDim1 = outShape[1]; outDim2 = outShape[2]
                outClassesFirst = outDim1 < outDim2              // 84 < N: classes-first
                interpreter = interp
                modelName = name.removeSuffix(".tflite")
                isAvailable = true
                Log.i(TAG, "session: model=$modelName ep=$provider input=$inputSize out=[${outDim1},${outDim2}]")
            } catch (t: Throwable) {
                lastError = t.message
                Log.e(TAG, "failed to load $name (tflite)", t)
                gpuDelegate?.close(); gpuDelegate = null
            }
        }
        square = Bitmap.createBitmap(inputSize, inputSize, Bitmap.Config.ARGB_8888)
        pixels = IntArray(inputSize * inputSize)
        inputBuf = ByteBuffer.allocateDirect(inputSize * inputSize * 3 * 4).order(ByteOrder.nativeOrder())
    }

    override fun detect(image: Image, rotationDegrees: Int): List<Detection> {
        val interp = interpreter ?: return emptyList()
        val rot = ((rotationDegrees % 360) + 360) % 360
        try {
            val t0 = android.os.SystemClock.elapsedRealtimeNanos()
            val src = toRgb(image)
            val t1 = android.os.SystemClock.elapsedRealtimeNanos()
            val sq = square!!
            val upW = if (rot == 90 || rot == 270) src.height else src.width
            val upH = if (rot == 90 || rot == 270) src.width else src.height
            val scale = minOf(inputSize / upW.toFloat(), inputSize / upH.toFloat())
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
            val buf = inputBuf!!
            buf.rewind()
            val n = inputSize * inputSize
            for (i in 0 until n) {                               // NHWC: R, G, B per pixel
                val p = pixels[i]
                buf.putFloat(((p shr 16) and 0xFF) / 255f)
                buf.putFloat(((p shr 8) and 0xFF) / 255f)
                buf.putFloat((p and 0xFF) / 255f)
            }
            buf.rewind()
            val t2 = android.os.SystemClock.elapsedRealtimeNanos()
            val output = Array(1) { Array(outDim1) { FloatArray(outDim2) } }
            interp.run(buf, output)
            val t3 = android.os.SystemClock.elapsedRealtimeNanos()
            val rows = toRows(output[0])
            val dets = decode(rows, rot)
            val t4 = android.os.SystemClock.elapsedRealtimeNanos()
            if (++frames % 24 == 0) Log.i(TAG, "[$provider] yuv ${(t1 - t0) / 1_000_000} ms, prep ${(t2 - t1) / 1_000_000} ms, run ${(t3 - t2) / 1_000_000} ms, decode ${(t4 - t3) / 1_000_000} ms, ${dets.size} boxes")
            return dets
        } catch (e: Throwable) {
            lastError = e.message
            Log.e(TAG, "detect failed (tflite)", e)
            return emptyList()
        }
    }

    /** Output tensor -> rows[4 + classes][anchors], transposing when the model is anchors-first. */
    private fun toRows(out: Array<FloatArray>): Array<FloatArray> {
        if (outClassesFirst) return out                          // [84][N] already
        val rowsN = out[0].size                                  // 84
        val anchors = out.size                                   // N
        return Array(rowsN) { r -> FloatArray(anchors) { a -> out[a][r] } }
    }

    /** `rows`: [4 + classes][anchors], cx cy w h in input pixels. Boxes in captured-image coords. */
    private fun decode(rows: Array<FloatArray>, rotationDegrees: Int): List<Detection> {
        val anchors = rows[0].size
        val classes = rows.size - 4
        // onnx2tf sometimes emits normalized (0..1) box coords; detect and scale to input pixels.
        val coordScale = if (maxCoord(rows, anchors) <= 1.5f) inputSize.toFloat() else 1f
        class Cand(val cls: Int, val score: Float, val x: Float, val y: Float, val w: Float, val h: Float)
        val cands = ArrayList<Cand>()
        for (j in 0 until anchors) {
            var best = -1; var bestScore = 0f
            for (c in 0 until classes) {
                val sc = rows[4 + c][j]
                if (sc > bestScore) { bestScore = sc; best = c }
            }
            if (best < 0 || best !in TrackedClass.phone || bestScore < TrackedClass.confidence(best)) continue
            val cx = rows[0][j] * coordScale; val cy = rows[1][j] * coordScale
            val bw = rows[2][j] * coordScale; val bh = rows[3][j] * coordScale
            val x0 = clampf((cx - bw / 2f - letterDx) / letterW, 0f, 1f)
            val y0 = clampf((cy - bh / 2f - letterDy) / letterH, 0f, 1f)
            val x1 = clampf((cx + bw / 2f - letterDx) / letterW, 0f, 1f)
            val y1 = clampf((cy + bh / 2f - letterDy) / letterH, 0f, 1f)
            if (x1 - x0 < 1e-3f || y1 - y0 < 1e-3f) continue
            cands.add(Cand(best, bestScore, x0, y0, x1 - x0, y1 - y0))
        }
        cands.sortByDescending { it.score }
        val kept = ArrayList<Cand>()
        for (c in cands) {
            if (kept.size >= 50) break
            var suppressed = false
            for (k in kept) if (k.cls == c.cls && iou(k.x, k.y, k.w, k.h, c.x, c.y, c.w, c.h) > YoloOnnxDetector.IOU_THRESHOLD) { suppressed = true; break }
            if (!suppressed) kept.add(c)
        }
        return kept.map { Detection(it.cls, YoloOnnxDetector.capturedImageRect(RectF(it.x, it.y, it.w, it.h), rotationDegrees), it.score) }
    }

    private fun maxCoord(rows: Array<FloatArray>, anchors: Int): Float {
        var mx = 0f
        for (r in 0 until 4) { val a = rows[r]; for (j in 0 until anchors) if (a[j] > mx) mx = a[j] }
        return mx
    }

    private fun iou(ax: Float, ay: Float, aw: Float, ah: Float, bx: Float, by: Float, bw: Float, bh: Float): Float {
        val ix = maxOf(0f, minOf(ax + aw, bx + bw) - maxOf(ax, bx))
        val iy = maxOf(0f, minOf(ay + ah, by + bh) - maxOf(ay, by))
        val inter = ix * iy
        val union = aw * ah + bw * bh - inter
        return if (union <= 0f) 0f else inter / union
    }

    private fun clampf(v: Float, lo: Float, hi: Float) = if (v < lo) lo else if (v > hi) hi else v

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

    fun close() {
        interpreter?.close(); interpreter = null
        gpuDelegate?.close(); gpuDelegate = null
    }
}
