package dev.minband.android

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Matrix
import android.media.Image
import android.util.Log
import io.clika.modelverse.DetectOptions
import io.clika.modelverse.Detection as ClikaDetection
import io.clika.modelverse.ImageInput
import io.clika.modelverse.Modelverse
import io.clika.modelverse.ModelverseException
import io.clika.modelverse.VisionListener
import io.clika.modelverse.VisionLoadOptions
import io.clika.modelverse.VisionModel
import io.clika.modelverse.VisionReport
import java.nio.ByteBuffer
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * ClikaRT on-device detector: same `ustc-community/dfine-nano-coco` model pushed bitwise-identical
 * to the laptop's edge/detector.py cache dir (docs/CLIKA_PLAN.md Phase 2.4, SC1 "identical model"
 * bonus). `VisionModel.detect()` is async (listener callback); [detect] blocks the calling thread
 * until the listener fires, matching [Detector]'s synchronous, single-thread contract.
 *
 * Model.detect() returns boxes already decoded in *pixel* coordinates of the image passed in
 * (confirmed via clika_runtime's Detection.xmin/ymin/xmax/ymax docstring, same decode as the
 * desktop path -- see docs/CLIKA_PLAN.md Session 2 log). No tiling here: unlike the hub's 4K drone
 * footage, the phone's own camera frame is small enough that whole-frame detection is enough.
 */
class ClikaDetector(context: Context) : Detector {
    companion object {
        private const val TAG = "MinBand Clika"
        private const val MODEL_ID = "ustc-community/dfine-nano-coco"
        // Pushed by `adb push tools/footage/models/clika/<snapshot> /data/local/tmp/clikart/model`.
        private const val CACHE_DIR = "/data/local/tmp/clikart/model"
        private const val CONFIDENCE_THRESHOLD = 0.35f
        private const val MAX_DETECTIONS = 50
        private const val DETECT_TIMEOUT_SECONDS = 5L
    }

    override var isAvailable = false; private set
    override var modelName: String? = null; private set
    override var lastError: String? = null; private set

    private val model: VisionModel? = try {
        // ClikaRtAndroid.load() (MinBandApplication) only places the license and loads the base
        // ClikaRT runtime; the modelverse layer (AutoModel*, VisionModel) ships in its own native
        // libs and needs this separate, idempotent call first (else "Modelverse.load has not
        // succeeded in this process").
        Modelverse.load()
        val m = VisionModel.open(MODEL_ID, VisionLoadOptions(cacheDir = CACHE_DIR, offline = true, device = "cpu"))
        isAvailable = true
        modelName = MODEL_ID
        m
    } catch (e: Throwable) {
        lastError = e.message ?: e.toString()
        Log.e(TAG, "failed to open $MODEL_ID", e)
        null
    }

    private var sensor: Bitmap? = null // image-sized, sensor orientation
    private var sensorPixels = IntArray(0)
    private var upright: Bitmap? = null // upright-rotated, image-sized (no resize)
    private var rgbaBuffer: ByteArray = ByteArray(0)
    private var frames = 0

    private fun toRgb(image: Image): Bitmap {
        val w = image.width; val h = image.height
        var bmp = sensor
        if (bmp == null || bmp.width != w || bmp.height != h) {
            bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888); sensor = bmp
            if (sensorPixels.size != w * h) sensorPixels = IntArray(w * h)
        }
        YuvToRgb.convert(image, sensorPixels)
        bmp.setPixels(sensorPixels, 0, w, 0, 0, w, h)
        return bmp
    }

    override fun detect(image: Image, rotationDegrees: Int): List<Detection> {
        val m = model ?: return emptyList()
        val rot = ((rotationDegrees % 360) + 360) % 360
        try {
            val src = toRgb(image)
            val upW = if (rot == 90 || rot == 270) src.height else src.width
            val upH = if (rot == 90 || rot == 270) src.width else src.height
            var up = upright
            if (up == null || up.width != upW || up.height != upH) {
                up = Bitmap.createBitmap(upW, upH, Bitmap.Config.ARGB_8888); upright = up
            }
            val m2 = Matrix()
            m2.postTranslate(-src.width / 2f, -src.height / 2f)
            m2.postRotate(rot.toFloat())
            m2.postTranslate(upW / 2f, upH / 2f)
            Canvas(up).drawBitmap(src, m2, null)

            val needed = upW * upH * 4
            if (rgbaBuffer.size != needed) rgbaBuffer = ByteArray(needed)
            up.copyPixelsToBuffer(ByteBuffer.wrap(rgbaBuffer))

            val input = ImageInput.Rgba8888(rgbaBuffer, upW, upH)
            val options = DetectOptions(maxResults = MAX_DETECTIONS, scoreThreshold = CONFIDENCE_THRESHOLD, labels = null)
            val latch = CountDownLatch(1)
            var result: List<ClikaDetection> = emptyList()
            m.detect(input, options, object : VisionListener {
                override fun onDetections(detections: List<ClikaDetection>) { result = detections }
                override fun onDone(report: VisionReport) { latch.countDown() }
                override fun onError(message: String) { lastError = message; latch.countDown() }
            })
            if (!latch.await(DETECT_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
                lastError = "detect timed out"
                return emptyList()
            }
            if (++frames % 30 == 0) Log.i(TAG, "raw ${result.size}: ${result.joinToString { "${it.label}=${it.score}" }}")
            return result.mapNotNull { d ->
                val classId = TrackedClass.classId(d.label) ?: return@mapNotNull null
                val box = d.box
                val rect = RectF(box.left / upW, box.top / upH, (box.right - box.left) / upW, (box.bottom - box.top) / upH)
                Detection(classId, YoloOnnxDetector.capturedImageRect(rect, rot), d.score)
            }
        } catch (e: ModelverseException) {
            lastError = e.message
            Log.e(TAG, "detect failed", e)
            return emptyList()
        } catch (e: Throwable) {
            lastError = e.message
            Log.e(TAG, "detect failed", e)
            return emptyList()
        }
    }
}
