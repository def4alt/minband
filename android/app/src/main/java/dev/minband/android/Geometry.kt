package dev.minband.android

import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

// Small float geometry: the subset of simd the iOS perception code uses, so Origin, Lift3D and
// Tracker port line for line. Matrices are column-major 4x4 (OpenGL / ARCore `Pose.toMatrix`),
// the same layout as simd_float4x4's columns.

data class Vec3(val x: Float, val y: Float, val z: Float) {
    operator fun plus(o: Vec3) = Vec3(x + o.x, y + o.y, z + o.z)
    operator fun minus(o: Vec3) = Vec3(x - o.x, y - o.y, z - o.z)
    operator fun times(s: Float) = Vec3(x * s, y * s, z * s)
    operator fun div(s: Float) = Vec3(x / s, y / s, z / s)
    operator fun unaryMinus() = Vec3(-x, -y, -z)
    fun dot(o: Vec3) = x * o.x + y * o.y + z * o.z
    fun cross(o: Vec3) = Vec3(y * o.z - z * o.y, z * o.x - x * o.z, x * o.y - y * o.x)
    val length: Float get() = sqrt(x * x + y * y + z * z)
    fun normalized(): Vec3 { val l = length; return if (l > 0f) this / l else this }
    fun distance(o: Vec3) = (this - o).length
    fun toList() = listOf(x, y, z)

    companion object {
        val ZERO = Vec3(0f, 0f, 0f)
        val UP = Vec3(0f, 1f, 0f)
    }
}

data class Vec2(val x: Float, val y: Float) {
    operator fun plus(o: Vec2) = Vec2(x + o.x, y + o.y)
    operator fun times(s: Float) = Vec2(x * s, y * s)
    val length: Float get() = sqrt(x * x + y * y)
}

/** Unit quaternion (x, y, z, w), w last like simd_quatf.vector and the wire. */
data class Quat(val x: Float, val y: Float, val z: Float, val w: Float) {
    operator fun times(o: Quat) = Quat(
        w * o.x + x * o.w + y * o.z - z * o.y,
        w * o.y - x * o.z + y * o.w + z * o.x,
        w * o.z + x * o.y - y * o.x + z * o.w,
        w * o.w - x * o.x - y * o.y - z * o.z,
    )

    fun normalized(): Quat {
        val l = sqrt(x * x + y * y + z * z + w * w)
        return if (l > 0f) Quat(x / l, y / l, z / l, w / l) else IDENTITY
    }

    /** Rotate `v`. */
    fun act(v: Vec3): Vec3 {
        val u = Vec3(x, y, z)
        val t = u.cross(v) * 2f
        return v + t * w + u.cross(t)
    }

    /** Same rotation with w >= 0 (q and -q are equal; the wire wants one spelling). */
    fun canonical(): Quat = if (w < 0f) Quat(-x, -y, -z, -w) else this

    companion object {
        val IDENTITY = Quat(0f, 0f, 0f, 1f)

        fun axisAngle(axis: Vec3, angle: Float): Quat {
            val a = axis.normalized()
            val s = sin(angle / 2f)
            return Quat(a.x * s, a.y * s, a.z * s, cos(angle / 2f))
        }

        /** From the rotation part of a rigid column-major matrix (orthonormal columns). */
        fun fromMatrix(m: Mat4): Quat {
            val m00 = m[0]; val m10 = m[1]; val m20 = m[2]
            val m01 = m[4]; val m11 = m[5]; val m21 = m[6]
            val m02 = m[8]; val m12 = m[9]; val m22 = m[10]
            val tr = m00 + m11 + m22
            val q = if (tr > 0f) {
                val s = sqrt(tr + 1f) * 2f
                Quat((m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s, 0.25f * s)
            } else if (m00 > m11 && m00 > m22) {
                val s = sqrt(1f + m00 - m11 - m22) * 2f
                Quat(0.25f * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s)
            } else if (m11 > m22) {
                val s = sqrt(1f + m11 - m00 - m22) * 2f
                Quat((m01 + m10) / s, 0.25f * s, (m12 + m21) / s, (m02 - m20) / s)
            } else {
                val s = sqrt(1f + m22 - m00 - m11) * 2f
                Quat((m02 + m20) / s, (m12 + m21) / s, 0.25f * s, (m10 - m01) / s)
            }
            return q.normalized()
        }
    }
}

/** Column-major 4x4, 16 floats: element (row r, column c) is `v[c * 4 + r]`. */
class Mat4(val v: FloatArray = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = 1f; it[15] = 1f }) {
    init { require(v.size == 16) }

    operator fun get(i: Int) = v[i]

    fun column(c: Int) = Vec3(v[c * 4], v[c * 4 + 1], v[c * 4 + 2])
    val x: Vec3 get() = column(0)
    val y: Vec3 get() = column(1)
    val z: Vec3 get() = column(2)
    val origin: Vec3 get() = column(3)

    operator fun times(o: Mat4): Mat4 {
        val r = FloatArray(16)
        for (c in 0 until 4) for (row in 0 until 4) {
            var s = 0f
            for (k in 0 until 4) s += v[k * 4 + row] * o.v[c * 4 + k]
            r[c * 4 + row] = s
        }
        return Mat4(r)
    }

    fun transformPoint(p: Vec3) = Vec3(
        v[0] * p.x + v[4] * p.y + v[8] * p.z + v[12],
        v[1] * p.x + v[5] * p.y + v[9] * p.z + v[13],
        v[2] * p.x + v[6] * p.y + v[10] * p.z + v[14],
    )

    fun transformDirection(d: Vec3) = Vec3(
        v[0] * d.x + v[4] * d.y + v[8] * d.z,
        v[1] * d.x + v[5] * d.y + v[9] * d.z,
        v[2] * d.x + v[6] * d.y + v[10] * d.z,
    )

    /** Exact inverse of a rotation + translation. */
    fun inverseRigid(): Mat4 {
        val r = FloatArray(16)
        // Transpose the rotation.
        for (c in 0 until 3) for (row in 0 until 3) r[c * 4 + row] = v[row * 4 + c]
        val t = origin
        r[12] = -(r[0] * t.x + r[4] * t.y + r[8] * t.z)
        r[13] = -(r[1] * t.x + r[5] * t.y + r[9] * t.z)
        r[14] = -(r[2] * t.x + r[6] * t.y + r[10] * t.z)
        r[15] = 1f
        return Mat4(r)
    }

    companion object {
        val IDENTITY get() = Mat4()

        fun fromColumns(x: Vec3, y: Vec3, z: Vec3, o: Vec3) = Mat4(floatArrayOf(
            x.x, x.y, x.z, 0f,
            y.x, y.y, y.z, 0f,
            z.x, z.y, z.z, 0f,
            o.x, o.y, o.z, 1f,
        ))

        fun fromRotation(q: Quat, o: Vec3 = Vec3.ZERO): Mat4 =
            fromColumns(q.act(Vec3(1f, 0f, 0f)), q.act(Vec3(0f, 1f, 0f)), q.act(Vec3(0f, 0f, 1f)), o)
    }
}

internal fun clampf(v: Float, lo: Float, hi: Float) = if (v < lo) lo else if (v > hi) hi else v
internal fun safeAcos(c: Float) = acos(clampf(c, -1f, 1f))
internal fun absf(v: Float) = abs(v)
