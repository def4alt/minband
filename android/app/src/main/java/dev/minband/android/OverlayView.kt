package dev.minband.android

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Typeface
import android.os.SystemClock
import android.view.View

/**
 * Detection boxes over the camera: four corner brackets per box, `CLASS · id` label above the
 * top-left corner, a `+` at the lifted 3D point. Solid white when the box feeds a confirmed track,
 * dotted and dimmer when tentative or when the detector has not updated for 0.5 s (docs/STYLE.md).
 */
class OverlayView(context: Context) : View(context) {
    private var boxes: List<OverlayBox> = emptyList()
    private var marks: List<LiftMark> = emptyList()
    private var updatedAt = 0L
    var showConfidence = false

    private val dp = resources.displayMetrics.density
    private val solid = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; style = Paint.Style.STROKE; strokeWidth = 1f * dp }
    private val dotted = Paint(solid).apply { color = Color.argb(140, 255, 255, 255); pathEffect = DashPathEffect(floatArrayOf(2f * dp, 3f * dp), 0f) }
    private val label = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; typeface = Typeface.MONOSPACE; textSize = 11f * dp; letterSpacing = 0.08f }
    private val labelDim = Paint(label).apply { color = Color.argb(140, 255, 255, 255) }
    private val tick = 10f * dp

    fun set(o: Pipeline.Overlay) {
        boxes = o.boxes; marks = o.marks
        updatedAt = SystemClock.uptimeMillis()
        invalidate()
        postDelayed({ invalidate() }, 550)
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat(); val h = height.toFloat()
        val stale = SystemClock.uptimeMillis() - updatedAt > 500
        for (b in boxes) {
            val confirmed = b.trackId != null && !stale
            val p = if (confirmed) solid else dotted
            val x0 = b.rect.x * w; val y0 = b.rect.y * h; val x1 = b.rect.maxX * w; val y1 = b.rect.maxY * h
            val t = minOf(tick, (x1 - x0) / 3f, (y1 - y0) / 3f)
            // Corner brackets.
            canvas.drawLine(x0, y0, x0 + t, y0, p); canvas.drawLine(x0, y0, x0, y0 + t, p)
            canvas.drawLine(x1, y0, x1 - t, y0, p); canvas.drawLine(x1, y0, x1, y0 + t, p)
            canvas.drawLine(x0, y1, x0 + t, y1, p); canvas.drawLine(x0, y1, x0, y1 - t, p)
            canvas.drawLine(x1, y1, x1 - t, y1, p); canvas.drawLine(x1, y1, x1, y1 - t, p)
            val name = TrackedClass.name(b.classId).uppercase()
            val id = b.trackId?.let { String.format("%02d", it) } ?: "--"
            val text = if (showConfidence) "$name · $id · ${String.format("%.2f", b.conf)}" else "$name · $id"
            canvas.drawText(text, x0, y0 - 4f * dp, if (confirmed) label else labelDim)
        }
        for (m in marks) {
            val x = m.x * w; val y = m.y * h
            val r = 4f * dp
            canvas.drawLine(x - r, y, x + r, y, solid); canvas.drawLine(x, y - r, x, y + r, solid)
        }
    }
}
