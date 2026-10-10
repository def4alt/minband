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
 * Detection boxes over the camera: four corner brackets per box with a dark halo so they read on a
 * light or busy background, a `CLASS · id` label in a filled chip above the top-left corner, a `+`
 * at the lifted 3D point. A confirmed track is a solid box; a moving track is amber and its chip
 * leads with `MOVING`; a tentative box (no track yet) or one the detector has not refreshed for
 * 0.5 s is dotted and dim (docs/STYLE.md, but contrast wins where the two fight).
 */
class OverlayView(context: Context) : View(context) {
    private var boxes: List<OverlayBox> = emptyList()
    private var marks: List<LiftMark> = emptyList()
    private var updatedAt = 0L
    var showConfidence = false

    private val dp = resources.displayMetrics.density
    private val moving = Color.rgb(255, 194, 77)      // amber accent for a mover
    private val white = Color.WHITE

    // A dark halo under every stroke, drawn first and wider, so a white/amber line never vanishes.
    private val halo = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(170, 0, 0, 0); style = Paint.Style.STROKE; strokeWidth = 4.5f * dp; strokeCap = Paint.Cap.ROUND }
    private val solid = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = white; style = Paint.Style.STROKE; strokeWidth = 2.2f * dp; strokeCap = Paint.Cap.ROUND }
    private val solidMoving = Paint(solid).apply { color = moving; strokeWidth = 2.8f * dp }
    private val dotted = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(150, 255, 255, 255); style = Paint.Style.STROKE; strokeWidth = 1.6f * dp; pathEffect = DashPathEffect(floatArrayOf(2f * dp, 3f * dp), 0f) }

    private val chip = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(190, 0, 0, 0); style = Paint.Style.FILL }
    private val chipMoving = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = moving; style = Paint.Style.FILL }
    private val labelText = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = white; typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD); textSize = 12.5f * dp; letterSpacing = 0.06f }
    private val labelTextOnAmber = Paint(labelText).apply { color = Color.rgb(12, 12, 12) }
    private val labelDim = Paint(labelText).apply { color = Color.argb(160, 255, 255, 255); typeface = Typeface.MONOSPACE }

    private val tick = 14f * dp

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
            val stroke = when { !confirmed -> dotted; b.moving -> solidMoving; else -> solid }
            val x0 = b.rect.x * w; val y0 = b.rect.y * h; val x1 = b.rect.maxX * w; val y1 = b.rect.maxY * h
            val t = minOf(tick, (x1 - x0) / 3f, (y1 - y0) / 3f)
            if (confirmed) brackets(canvas, x0, y0, x1, y1, t, halo)   // halo only under solid boxes
            brackets(canvas, x0, y0, x1, y1, t, stroke)

            val name = TrackedClass.name(b.classId).uppercase()
            val id = b.trackId?.let { String.format("%02d", it) } ?: "--"
            val base = if (showConfidence) "$name · $id · ${String.format("%.2f", b.conf)}" else "$name · $id"
            val text = if (b.moving) "MOVING · $base" else base
            drawLabel(canvas, text, x0, y0, confirmed, b.moving, h)
        }
        for (m in marks) {
            val x = m.x * w; val y = m.y * h; val r = 5f * dp
            canvas.drawLine(x - r, y, x + r, y, halo); canvas.drawLine(x, y - r, x, y + r, halo)
            canvas.drawLine(x - r, y, x + r, y, solid); canvas.drawLine(x, y - r, x, y + r, solid)
        }
    }

    private fun brackets(c: Canvas, x0: Float, y0: Float, x1: Float, y1: Float, t: Float, p: Paint) {
        c.drawLine(x0, y0, x0 + t, y0, p); c.drawLine(x0, y0, x0, y0 + t, p)
        c.drawLine(x1, y0, x1 - t, y0, p); c.drawLine(x1, y0, x1, y0 + t, p)
        c.drawLine(x0, y1, x0 + t, y1, p); c.drawLine(x0, y1, x0, y1 - t, p)
        c.drawLine(x1, y1, x1 - t, y1, p); c.drawLine(x1, y1, x1, y1 - t, p)
    }

    private fun drawLabel(c: Canvas, text: String, x0: Float, y0: Float, confirmed: Boolean, moving: Boolean, viewH: Float) {
        val tp = when { !confirmed -> labelDim; moving -> labelTextOnAmber; else -> labelText }
        val fm = tp.fontMetrics
        val padX = 5f * dp; val padY = 3f * dp
        val tw = tp.measureText(text)
        val chipH = (fm.descent - fm.ascent) + padY * 2
        val gap = 3f * dp
        // Above the box, or below it when there is no room above.
        var top = y0 - gap - chipH
        if (top < 0f) top = (y0 + gap).coerceAtMost(viewH - chipH)
        val left = x0.coerceAtLeast(0f)
        val bg = when { !confirmed -> chip; moving -> chipMoving; else -> chip }
        val rect = android.graphics.RectF(left, top, left + tw + padX * 2, top + chipH)
        c.drawRoundRect(rect, 3f * dp, 3f * dp, bg)
        c.drawText(text, left + padX, top + padY - fm.ascent, tp)
    }
}
