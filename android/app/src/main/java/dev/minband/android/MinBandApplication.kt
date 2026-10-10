package dev.minband.android

import android.app.Application
import android.util.Log
import io.clika.runtime.ClikaRtAndroid
import java.io.File

/**
 * Loads the ClikaRT license before any detector touches the runtime. The key itself never
 * ships in the app or the repo: it's a file dropped into app-private storage (OS-sandboxed,
 * no extra encryption needed for a hackathon device) by an out-of-band step --
 *   adb push credential.txt /data/local/tmp/clika_license.txt
 *   adb shell run-as dev.minband.android cp /data/local/tmp/clika_license.txt files/clika_license.txt
 * -- never committed, never logged. Missing file means detection falls back to whatever
 * Detector.lastError reports; it does not crash the app.
 */
class MinBandApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        val license = readCredential()
        if (license != null) {
            try {
                ClikaRtAndroid.load(this, license)
            } catch (e: Throwable) {
                Log.e(TAG, "ClikaRT license load failed", e)
            }
        } else {
            Log.w(TAG, "no ClikaRT license at ${licenseFile().path}; ClikaRT detector unavailable")
        }
    }

    private fun licenseFile(): File = File(filesDir, "clika_license.txt")

    private fun readCredential(): String? =
        licenseFile().takeIf { it.exists() }?.readText()?.trim()?.takeIf { it.isNotEmpty() }

    companion object {
        private const val TAG = "MinBandApplication"
    }
}
