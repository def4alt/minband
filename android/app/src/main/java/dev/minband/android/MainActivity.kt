package dev.minband.android

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.opengl.GLSurfaceView
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.Surface
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import com.google.ar.core.ArCoreApk
import com.google.ar.core.exceptions.UnavailableException

/**
 * The screen (ios/MinBand/ContentView.swift + UI/): camera with detection overlay, a HUD band on
 * top (LINK, TRACKS, ORIGIN), START / ORIGIN HERE / HOST at the bottom. Monochrome on #070809,
 * hairlines, no icons (docs/STYLE.md). Portrait only.
 */
class MainActivity : Activity(), Pipeline.Listener {
    private lateinit var pipeline: Pipeline
    private lateinit var glView: GLSurfaceView
    private lateinit var overlay: OverlayView
    private lateinit var linkValue: TextView
    private lateinit var tracksValue: TextView
    private lateinit var originValue: TextView
    private lateinit var hostLine: TextView
    private lateinit var statusLine: TextView
    private lateinit var details: TextView
    private lateinit var startButton: Button
    private lateinit var originButton: Button
    private lateinit var hostRow: LinearLayout
    private lateinit var hostField: EditText
    private var installRequested = false
    private var state = Pipeline.UiState()
    private var showDetails = false
    private var blinkOn = true

    private val bg = Color.parseColor("#070809")
    private val ink = Color.parseColor("#E6E7E9")
    private val ink2 = Color.parseColor("#9A9DA3")
    private val ink3 = Color.parseColor("#5D6066")
    private val hairline = Color.parseColor("#2A2D33")

    private val prefs by lazy { getSharedPreferences("minband", Context.MODE_PRIVATE) }
    private var host: String
        get() = prefs.getString("host", DEFAULT_HOST) ?: DEFAULT_HOST
        set(v) { prefs.edit().putString("host", v).apply() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.statusBarColor = bg
        window.navigationBarColor = bg
        pipeline = Pipeline(this, this)

        val dp = resources.displayMetrics.density
        fun px(v: Float) = (v * dp).toInt()

        val root = FrameLayout(this).apply { setBackgroundColor(bg) }
        glView = GLSurfaceView(this).apply {
            preserveEGLContextOnPause = true
            setEGLContextClientVersion(2)
            setEGLConfigChooser(8, 8, 8, 8, 16, 0)
            setRenderer(ArRenderer(pipeline) { displayRotation() })
            renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY
        }
        root.addView(glView, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        overlay = OverlayView(this)
        root.addView(overlay, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        // HUD band.
        val hud = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(bg)
            setPadding(px(16f), px(12f), px(16f), px(10f))
        }
        val titleRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        titleRow.addView(label("MINBAND", ink, 12f, bold = true), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        val detailsToggle = label("DETAILS", ink3, 10f).apply {
            setOnClickListener { showDetails = !showDetails; this@MainActivity.overlay.showConfidence = showDetails; render() }
            setPadding(px(8f), 0, 0, px(4f))
        }
        titleRow.addView(detailsToggle)
        hud.addView(titleRow)
        val values = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; setPadding(0, px(8f), 0, 0) }
        linkValue = value(); tracksValue = value(); originValue = value().apply { setTextColor(ink2) }
        values.addView(column("LINK", linkValue), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1.2f))
        values.addView(column("TRACKS", tracksValue), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        values.addView(column("ORIGIN", originValue), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        hud.addView(values)
        hostLine = label("", ink3, 10f).apply { setPadding(0, px(6f), 0, 0) }
        hud.addView(hostLine)
        statusLine = label("", ink2, 10f).apply { visibility = View.GONE; setPadding(0, px(6f), 0, 0) }
        hud.addView(statusLine)
        details = TextView(this).apply {
            typeface = Typeface.MONOSPACE; textSize = 10f; setTextColor(ink2); visibility = View.GONE
            setPadding(0, px(8f), 0, 0); setLineSpacing(0f, 1.25f)
        }
        hud.addView(details)
        hud.addView(View(this).apply { setBackgroundColor(hairline) }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 1).apply { topMargin = px(10f) })
        root.addView(hud, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP))

        // Bottom: host row (hidden) above the bar.
        val bottom = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        hostRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; setBackgroundColor(bg); visibility = View.GONE
            setPadding(px(16f), px(8f), px(16f), px(8f)); gravity = Gravity.CENTER_VERTICAL
        }
        hostRow.addView(label("HOST", ink3, 10f).apply { setPadding(0, 0, px(12f), 0) })
        hostField = EditText(this).apply {
            setText(host); typeface = Typeface.MONOSPACE; textSize = 13f; setTextColor(ink); setHintTextColor(ink3)
            hint = "192.168.1.10:7777"; inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            isSingleLine = true; background = null
            setOnEditorActionListener { _, _, _ -> commitHost(); true }
        }
        hostRow.addView(hostField, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        hostRow.addView(barButton("DONE") { commitHost() })
        bottom.addView(hostRow)
        bottom.addView(View(this).apply { setBackgroundColor(hairline) }, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 1))
        val bar = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; setBackgroundColor(bg); setPadding(px(8f), px(6f), px(8f), px(10f)) }
        startButton = barButton("START") { toggleStart() }
        originButton = barButton("ORIGIN HERE") { glView.queueEvent { pipeline.setOriginHere() } }
        val hostButton = barButton("HOST") { hostRow.visibility = if (hostRow.visibility == View.VISIBLE) View.GONE else View.VISIBLE }
        for (b in listOf(startButton, originButton, hostButton)) bar.addView(b, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        bottom.addView(bar)
        root.addView(bottom, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))

        // Keep the HUD below the status bar and the bar above the gesture area.
        root.setOnApplyWindowInsetsListener { _, insets ->
            val top: Int; val bottomInset: Int
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                val sys = insets.getInsets(android.view.WindowInsets.Type.systemBars())
                top = sys.top; bottomInset = sys.bottom
            } else {
                @Suppress("DEPRECATION")
                run { top = insets.systemWindowInsetTop; bottomInset = insets.systemWindowInsetBottom }
            }
            hud.setPadding(px(16f), top + px(12f), px(16f), px(10f))
            bar.setPadding(px(8f), px(6f), px(8f), bottomInset + px(10f))
            insets
        }
        setContentView(root)
        render()
        // LINK `lost` blinks at 1 Hz, the only thing that does.
        root.post(object : Runnable {
            override fun run() { blinkOn = !blinkOn; if (state.link == Pipeline.LinkState.LOST) render(); root.postDelayed(this, 500) }
        })
    }

    private fun label(text: String, color: Int, size: Float, bold: Boolean = false) = TextView(this).apply {
        this.text = text; setTextColor(color); textSize = size; letterSpacing = 0.12f
        typeface = if (bold) Typeface.create(Typeface.DEFAULT, Typeface.NORMAL) else Typeface.DEFAULT
    }

    private fun value() = TextView(this).apply {
        setTextColor(ink); textSize = 22f; typeface = Typeface.MONOSPACE; text = "—"
        fontFeatureSettings = "tnum"
    }

    private fun column(title: String, v: TextView) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(label(title, ink3, 9f))
        addView(v)
    }

    private fun barButton(text: String, onClick: () -> Unit) = Button(this).apply {
        this.text = text; textSize = 11f; letterSpacing = 0.12f; setTextColor(ink); background = null
        isAllCaps = true; setOnClickListener { onClick() }
    }

    private fun commitHost() {
        host = hostField.text.toString().trim()
        hostRow.visibility = View.GONE
        render()
    }

    private fun toggleStart() {
        if (state.running) {
            glView.queueEvent { pipeline.stop() }
        } else {
            if (pipeline.session == null) { Toast.makeText(this, "ARCore not ready", Toast.LENGTH_SHORT).show(); return }
            val h = host
            glView.queueEvent { pipeline.start(h) }
        }
    }

    // Pipeline.Listener (main thread)

    override fun onState(s: Pipeline.UiState) { state = s; render() }
    override fun onOverlay(o: Pipeline.Overlay) { overlay.set(o) }

    private fun render() {
        val s = state
        val dash = "—"
        linkValue.text = when {
            !s.running -> dash
            s.link == Pipeline.LinkState.WAITING -> "waiting"
            s.link == Pipeline.LinkState.LOST -> if (blinkOn) "lost" else ""
            else -> String.format("%.1f kbps", s.wireBytesPerSec * 8 / 1000.0)
        }
        tracksValue.text = if (s.running) "${s.trackCount}" else dash
        originValue.text = if (s.running) (if (s.originLocked) "locked" else "searching") else dash
        hostLine.text = "to ${host.ifBlank { "no host" }}"
        statusLine.text = s.status.uppercase()
        statusLine.visibility = if (s.status.isBlank()) View.GONE else View.VISIBLE
        startButton.text = if (s.running) "STOP" else "START"
        originButton.isEnabled = s.running
        originButton.alpha = if (s.running) 1f else 0.4f
        details.visibility = if (showDetails) View.VISIBLE else View.GONE
        if (showDetails) {
            details.text = listOf(
                "SEQ ${s.seq}   θ x${String.format("%.2f", s.thetaScale)}   BUDGET ${if (s.budgetBps == 0L) "none" else "${s.budgetBps} bit/s"}",
                "FPS ${String.format("%.0f", s.fps)}   DETECT ${String.format("%.1f", s.detectHz)} Hz   DEPTH ${s.depthMode}",
                "WIRE ${s.wireBytesPerSec} B/s   CORE ${s.bytesPerSec} B/s",
                "${s.detectorStatus}   ORIGIN ${s.originSource}   DEVICE ${s.deviceId}",
            ).joinToString("\n")
        }
    }

    // Lifecycle: camera permission, ARCore install, session.

    override fun onResume() {
        super.onResume()
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), 1)
            return
        }
        try {
            when (ArCoreApk.getInstance().requestInstall(this, !installRequested)) {
                ArCoreApk.InstallStatus.INSTALL_REQUESTED -> { installRequested = true; return }
                ArCoreApk.InstallStatus.INSTALLED -> {}
                else -> {}
            }
            if (pipeline.session == null) pipeline.createSession()
            pipeline.resume()
            glView.onResume()
        } catch (e: UnavailableException) {
            state = state.copy(status = "ARCore unavailable: ${e.javaClass.simpleName}"); render()
        } catch (e: Exception) {
            state = state.copy(status = "AR error: ${e.message}"); render()
        }
    }

    override fun onPause() {
        super.onPause()
        if (pipeline.session != null) {
            glView.onPause()
            pipeline.pause()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        pipeline.shutdown()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (grantResults.isEmpty() || grantResults[0] != PackageManager.PERMISSION_GRANTED) {
            Toast.makeText(this, "Camera permission is required", Toast.LENGTH_LONG).show()
            finish()
        }
    }

    private fun displayRotation(): Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) display?.rotation ?: Surface.ROTATION_0
        else @Suppress("DEPRECATION") windowManager.defaultDisplay.rotation

    companion object {
        /** Server `host[:port]`; the HOST button changes it and the choice is remembered. */
        const val DEFAULT_HOST = "192.168.0.16:7777"
    }
}
