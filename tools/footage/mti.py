"""Moving-target indication (MTI): class-agnostic movers from frame differencing on a moving camera.

Appearance models miss what they were not trained on (camouflage, unusual vehicles, decoys look
right to them); motion does not care what a thing looks like. For each detection frame n:

1. register: frames n-k and n+k (k = `baseline` s, ~0.3 s) are aligned onto frame n with a
   homography from sparse optical flow (Shi-Tomasi corners, pyramidal LK with a forward-backward
   check, RANSAC), at a working width of ~1920 px. A homography is exact for a plane; the ground is
   the plane, and what sticks out of it (buildings, trees, poles) leaves parallax residue.
2. photometric: each warped frame gets a robust gain/offset fit onto frame n, so auto-exposure and
   global light changes do not difference.
3. tolerant difference: |frame n - warped| measured against the 3x3 min/max of the warped frame, so
   up to ~1 px of misregistration (2 px in 4K) costs nothing. Backward and forward differences.
4. noise-adaptive threshold: the noise of the plain difference is estimated per 32 px block
   (median absolute deviation, smoothed, floored), and the differences become z-scores, so
   compression noise, texture and residual parallax in vegetation raise their own threshold.
5. hysteresis: a pixel is moving if it differs from both the past and the future (AND, min of the
   two z-scores: no ghosts at the old or new position, no single-frame flicker) above `z_seed`; the
   blob grows into the union of both differences (OR) above `z_grow`, which is symmetric about the
   object's position at frame n, so its centre is not biased along the motion.
6. morphology (open 2x2, close 5x5), connected components, pixel-area limits.
7. static overlays (watermarks, HUD text, channel logos) stay put in the image while the scene moves:
   `overlay_mask()` finds them from the clip itself (no hand-drawn masks) and they are not differenced.

track.py (mti_filter) then filters the candidates on the ground (it has the ground model): size in
metres, aspect ratio, and persistence with net motion over consecutive detection frames, which is
what removes parallax from tall structures under a hovering or drifting camera (it differences, but
it does not go anywhere). A camera that translates fast makes tree tops and roof edges appear to
travel over the ground at a fraction of its own speed; those residual false movers are not
removed (README, battlefield section).

Output rows are the appearance cache's format, x1 y1 x2 y2 in frame pixels, with class 100
(MinBand's unclassified ground mover) and a score from the seed z-score; extra per-blob statistics
go to a second array.
"""
import math
import numpy as np
import cv2

MOVER = 100  # MinBand class id: unclassified ground mover (core/src/classes.rs)


def to_work(frame: np.ndarray, scale: float) -> np.ndarray:
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    if scale != 1.0: g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(g.astype(np.float32), (3, 3), 0.8)


class MTI:
    def __init__(self, work_width: int = 1920, z_seed: float = 5.0, z_grow: float = 3.0, block: int = 32,
                 min_area: int = 6, max_area_frac: float = 0.01, sigma_floor: float = 1.0):
        self.work_width, self.z_seed, self.z_grow, self.block = work_width, z_seed, z_grow, block
        self.min_area, self.max_area_frac, self.sigma_floor = min_area, max_area_frac, sigma_floor
        self.k3 = np.ones((3, 3), np.uint8)

    def scale_for(self, w: int) -> float:
        return min(1.0, self.work_width / w)

    # ---- registration ----------------------------------------------------------------------------
    def register(self, src: np.ndarray, dst: np.ndarray):
        """Homography mapping `src` (work px) onto `dst`, inlier count and inlier RMS (px)."""
        d8 = dst.astype(np.uint8); s8 = src.astype(np.uint8)
        p_dst = cv2.goodFeaturesToTrack(d8, 3000, 0.005, 10, blockSize=7)
        if p_dst is None or len(p_dst) < 50: return None, 0, float('nan')
        p_src, st, _ = cv2.calcOpticalFlowPyrLK(d8, s8, p_dst, None, winSize=(21, 21), maxLevel=4)
        back, st2, _ = cv2.calcOpticalFlowPyrLK(s8, d8, p_src, None, winSize=(21, 21), maxLevel=4)
        ok = (st[:, 0] == 1) & (st2[:, 0] == 1) & (np.linalg.norm((back - p_dst).reshape(-1, 2), axis=1) < 0.5)
        if ok.sum() < 40: return None, int(ok.sum()), float('nan')
        a, b = p_src[ok].reshape(-1, 2), p_dst[ok].reshape(-1, 2)
        H, inl = cv2.findHomography(a, b, cv2.RANSAC, 1.0, maxIters=4000, confidence=0.999)
        if H is None: return None, 0, float('nan')
        m = inl[:, 0].astype(bool)
        r = cv2.perspectiveTransform(a[m].reshape(-1, 1, 2), H).reshape(-1, 2) - b[m]
        return H, int(m.sum()), float(np.sqrt((r ** 2).sum(1).mean()))

    # ---- one side's difference -------------------------------------------------------------------
    def side(self, cur: np.ndarray, other: np.ndarray, H: np.ndarray):
        h, w = cur.shape
        warped = cv2.warpPerspective(other, H, (w, h), flags=cv2.INTER_LINEAR, borderValue=0)
        valid = cv2.warpPerspective(np.ones_like(cur, np.uint8), H, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        valid = cv2.erode(valid, np.ones((9, 9), np.uint8)) > 0
        # Robust gain/offset of the warped frame onto the current one (exposure, global light).
        ys, xs = np.nonzero(valid[::4, ::4]); a_ = warped[::4, ::4][ys, xs]; c_ = cur[::4, ::4][ys, xs]
        gain, off = 1.0, 0.0
        if len(a_) > 1000:
            A = np.column_stack([a_, np.ones_like(a_)])
            for _ in range(2):
                gain, off = np.linalg.lstsq(A, c_, rcond=None)[0]
                r = np.abs(A @ [gain, off] - c_); keep = r <= np.quantile(r, 0.9)
                A, c_ = A[keep], c_[keep]
            if not (0.5 < gain < 2.0): gain, off = 1.0, 0.0
        warped = warped * gain + off
        hi = cv2.dilate(warped, self.k3); lo = cv2.erode(warped, self.k3)
        tol = np.maximum(np.maximum(cur - hi, lo - cur), 0)
        plain = np.abs(cur - warped)
        plain[~valid] = np.nan
        sigma = self.noise(plain)
        z = tol / sigma
        z[~valid] = 0
        return z, valid, (float(gain), float(off))

    def noise(self, plain: np.ndarray) -> np.ndarray:
        """Per-block robust sigma of the plain difference (|d| median / 0.6745), smoothed, floored."""
        h, w = plain.shape; b = self.block
        hb, wb = h // b, w // b
        blocks = plain[:hb * b, :wb * b].reshape(hb, b, wb, b).transpose(0, 2, 1, 3).reshape(hb, wb, -1)
        with np.errstate(all='ignore'):
            med = np.nanmedian(blocks, axis=2) / 0.6745
        g = np.nanmedian(med) if np.isfinite(med).any() else self.sigma_floor
        med = np.where(np.isfinite(med), med, g)
        # A block's estimate is noisy: take the max with its neighbourhood's median, then floor.
        med = np.maximum(med, cv2.medianBlur(med.astype(np.float32), 3))
        med = np.maximum(med, max(self.sigma_floor, 0.5 * g))
        return cv2.resize(med.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)

    # ---- one detection frame ---------------------------------------------------------------------
    def detect(self, prev: np.ndarray, cur: np.ndarray, nxt: np.ndarray, overlay=None, debug: bool = False):
        """Candidate blobs at frame `cur` (work px): rows x1 y1 x2 y2 area zpeak zmean, plus registration stats.
        `overlay`: work-scale mask of static overlays (overlay_mask), ignored where it or its warped copies lie."""
        Hb, nb, rb = self.register(prev, cur)
        Hf, nf, rf = self.register(nxt, cur)
        stats = {'inliers_b': nb, 'inliers_f': nf, 'rms_b': rb, 'rms_f': rf}
        if Hb is None or Hf is None:
            stats['failed'] = True
            return np.zeros((0, 7), np.float32), stats, None
        zb, vb, gb = self.side(cur, prev, Hb)
        zf, vf, gf = self.side(cur, nxt, Hf)
        stats.update(gain_b=gb, gain_f=gf)
        valid = vb & vf
        if overlay is not None and overlay.any():
            h, w = cur.shape; o8 = overlay.astype(np.uint8)
            ov = overlay | (cv2.warpPerspective(o8, Hb, (w, h), flags=cv2.INTER_NEAREST) > 0) \
                | (cv2.warpPerspective(o8, Hf, (w, h), flags=cv2.INTER_NEAREST) > 0)
            valid &= ~ov
        z_and = np.where(valid, np.minimum(zb, zf), 0)
        z_or = np.where(valid, np.maximum(zb, zf), 0)
        seed = (z_and > self.z_seed).astype(np.uint8)
        grow = (z_or > self.z_grow).astype(np.uint8)
        grow = cv2.morphologyEx(grow, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        grow = cv2.morphologyEx(grow, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        grow |= seed
        n, lab, st, _ = cv2.connectedComponentsWithStats(grow, connectivity=8)
        has_seed = np.zeros(n, bool); has_seed[np.unique(lab[seed > 0])] = True
        max_area = self.max_area_frac * cur.size
        rows = []
        for i in range(1, n):
            x, y, w, h, area = st[i]
            if not has_seed[i] or area < self.min_area or area > max_area: continue
            m = lab[y:y + h, x:x + w] == i
            za = z_and[y:y + h, x:x + w][m]
            rows.append([x, y, x + w, y + h, area, float(za.max()), float(z_or[y:y + h, x:x + w][m].mean())])
        out = np.array(rows, np.float32).reshape(-1, 7)
        dbg = {'z_and': z_and, 'z_or': z_or, 'mask': grow, 'valid': valid} if debug else None
        return out, stats, dbg


def overlay_mask(grays, homs, scale, min_motion_px=8.0, min_area=40, min_range_px=60.0, min_present=0.8):
    """Static overlays (watermarks, HUD text, channel logos): structure that stays put in the image
    while the scene moves under it. `grays`: work-scale frames sampled across the clip, `homs`: their
    full-resolution homographies onto the reference frame. The mean of the frames in image
    coordinates blurs the moving scene and keeps an overlay sharp, even a faint semi-transparent one
    (averaging N frames lifts it by sqrt(N) over the scene); the mean in scene coordinates, warped
    back, does the opposite. Overlay = edges of the image-coordinate mean that are well above its
    median and twice those of the scene-coordinate mean. Label-free and per clip. With a static camera
    the two means are the same and nothing is masked (an overlay then does not difference either).
    Returns a work-scale bool mask."""
    h, w = grays[0].shape
    S = np.diag([scale, scale, 1.0]); Si = np.linalg.inv(S)
    Hs = [S @ np.asarray(H, np.float64) @ Si for H in homs]
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
    warped = [cv2.perspectiveTransform(corners, H).reshape(-1, 2) for H in Hs]
    if np.median([np.abs(c - warped[0]).max() for c in warped]) < min_motion_px: return np.zeros((h, w), bool)
    allc = np.vstack(warped); x0, y0 = np.floor(allc.min(0)); x1, y1 = np.ceil(allc.max(0))
    cw, ch = int(min(x1 - x0, 4 * w)), int(min(y1 - y0, 4 * h))
    T = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], np.float64)
    acc = np.zeros((ch, cw), np.float32); cov = np.zeros((ch, cw), np.float32)
    for g, H in zip(grays, Hs):
        acc += cv2.warpPerspective(g, T @ H, (cw, ch)); cov += cv2.warpPerspective(np.ones_like(g), T @ H, (cw, ch))
    scene = acc / np.maximum(cov, 1e-3)
    m_img = np.mean(grays, axis=0)
    m_back = np.mean([cv2.warpPerspective(scene, np.linalg.inv(T @ H), (w, h)) for H in Hs], axis=0)
    def grad(m): return np.hypot(cv2.Sobel(m, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(m, cv2.CV_32F, 0, 1, ksize=3)) / 8
    gi, gb = grad(m_img), grad(m_back)
    # Only where the ground under a pixel moved far over the clip can the scene have blurred out of
    # the image mean (a slow drift leaves parallax and buildings sharp in it, and an overlay barely
    # moves between differenced frames then anyway).
    ys, xs = np.mgrid[0:h:8, 0:w:8]
    pts = np.column_stack([xs.ravel(), ys.ravel()]).astype(np.float32).reshape(-1, 1, 2)
    gp = np.stack([cv2.perspectiveTransform(pts, H).reshape(-1, 2) for H in Hs])  # frames x points x 2
    rng_ = np.hypot(*(gp.max(0) - gp.min(0)).T).reshape(xs.shape).astype(np.float32)
    rng_ = cv2.resize(rng_, (w, h), interpolation=cv2.INTER_LINEAR)
    m = ((gi > max(2.0, 4 * np.median(gi))) & (gi > 2 * gb) & (rng_ >= min_range_px)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    # An overlay is in (nearly) every frame: each frame's gradient there agrees with the mean's.
    # A target the camera follows is sharp in the mean too, but only while it sits at that spot.
    gm = np.stack([cv2.Sobel(m_img, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(m_img, cv2.CV_32F, 0, 1, ksize=3)], -1)
    norm2 = (gm ** 2).sum(-1) + 1e-6
    agree = np.zeros((len(grays), n), np.float32)
    for i, g in enumerate(grays):
        gf = np.stack([cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)], -1)
        a = (gf * gm).sum(-1) / norm2  # ~1 where frame i has the mean's edge, ~0 +- noise where not
        strong = (lab > 0) & (gi > max(2.0, 4 * np.median(gi)))
        for c in range(1, n):
            sel = strong & (lab == c)
            if sel.any(): agree[i, c] = np.median(a[sel])
    keep = np.zeros(n, bool)
    keep[1:] = (st[1:, cv2.CC_STAT_AREA] >= min_area) & ((agree[:, 1:] > 0.5).mean(0) >= min_present)
    return cv2.dilate(keep[lab].astype(np.uint8), np.ones((5, 5), np.uint8)) > 0


def score(zpeak, z_seed=5.0):
    """Seed z-score -> (0, 1): 0.35 at z_seed + 2, ~0.8 by z_seed + 12."""
    return float(1 - math.exp(-(max(zpeak - z_seed, 0) + 1.0) / 6.0)) if zpeak > 0 else 0.0
