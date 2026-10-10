// ENU metres -> pixel of the drone clip. Plain ES module, works in the browser and in Node.
//
// Chain (see README "Projection chain"):
//   ENU (e, n)            page convention: east = tracks x, north = -tracks z, up = tracks y
//   -> tracks (x, z)      x = e, z = -n
//   -> ground (X, Y)      track.py's flat-ground model, shifted by `origin` (median detection
//                          position): tracks.csv wrote x = X - origin[0], z = -(Y - origin[1]), so
//                          X = x + origin[0], Y = -z + origin[1] = n + origin[1]
//   -> reference pixel    G = K [r1 r2 t0] (planar homography of the reference frame's camera)
//   -> frame-n pixel      inv(H_n) where detect.json's H_n maps frame n ONTO the reference frame
//                          (frame start_frame). H is linearly interpolated between the two
//                          registered frames around the current one (every 6th frame has one).
//
// /meta (served by the mock, and by the real driver) is:
//   { fps, width, height, start_frame, end_frame, every,
//     ground: { f_px, pitch_deg, height_m, cx, cy }, origin: [ox, oy],
//     homographies: { "450": [[..],[..],[..]], ... } }

function mat3mulv(m, v) {
  return [
    m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
    m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
    m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
  ];
}

export function inv3(m) {
  const [a, b, c] = m[0], [d, e, f] = m[1], [g, h, i] = m[2];
  const A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
  const det = a * A + b * B + c * C;
  if (Math.abs(det) < 1e-12) return null;
  const s = 1 / det;
  return [
    [A * s, -(b * i - c * h) * s, (b * f - c * e) * s],
    [B * s, (a * i - c * g) * s, -(a * f - c * d) * s],
    [C * s, -(a * h - b * g) * s, (a * e - b * d) * s],
  ];
}

function dehom(p) {
  if (!(Math.abs(p[2]) > 1e-9)) return null;
  const u = p[0] / p[2], v = p[1] / p[2];
  return Number.isFinite(u) && Number.isFinite(v) ? { u, v } : null;
}

export function makeCamera(meta) {
  const g = meta.ground;
  const f = g.f_px, cx = g.cx ?? meta.width / 2, cy = g.cy ?? meta.height / 2;
  const p = (g.pitch_deg * Math.PI) / 180, h = g.height_m;
  const sp = Math.sin(p), cp = Math.cos(p);
  const [ox, oy] = meta.origin;
  // G = K @ [R0[:,0], R0[:,1], t0], R0 = [[1,0,0],[0,-sp,-cp],[0,cp,-sp]], t0 = -R0 @ [0,0,h]
  // r1 = [1,0,0], r2 = [0,-sp,cp], t0 = [0, h cp, h sp]
  const G = [
    [f, cx * 0 + 0, cx * 0 + 0], // filled below
    [0, 0, 0],
    [0, 0, 0],
  ];
  const K = [[f, 0, cx], [0, f, cy], [0, 0, 1]];
  const M = [[1, 0, 0], [0, -sp, h * cp], [0, cp, h * sp]];
  for (let r = 0; r < 3; r++) for (let c = 0; c < 3; c++) G[r][c] = K[r][0] * M[0][c] + K[r][1] * M[1][c] + K[r][2] * M[2][c];

  const fps = meta.fps, f0 = meta.start_frame, every = meta.every || 6;
  const frames = Object.keys(meta.homographies).map(Number).sort((a, b) => a - b);
  const Hinv = new Map();
  for (const n of frames) {
    const H = meta.homographies[String(n)];
    const s = H[2][2] || 1;
    const Hn = H.map((row) => row.map((x) => x / s));
    const inv = inv3(Hn);
    if (inv) Hinv.set(n, inv);
  }
  const lastFrame = frames[frames.length - 1];

  // inverse homography for an arbitrary frame index (fractional allowed): linear blend of the two
  // registered neighbours' inverses (normalised), which is accurate for the small motion in 6 frames.
  function hinvAt(frame) {
    if (frame <= frames[0]) return Hinv.get(frames[0]);
    if (frame >= lastFrame) return Hinv.get(lastFrame);
    const k = Math.floor((frame - f0) / every);
    const n0 = f0 + k * every, n1 = n0 + every;
    const A = Hinv.get(n0), B = Hinv.get(n1);
    if (!A) return B || null;
    if (!B) return A;
    const w = (frame - n0) / every;
    const out = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
    for (let r = 0; r < 3; r++) for (let c = 0; c < 3; c++) out[r][c] = A[r][c] * (1 - w) + B[r][c] * w;
    return out;
  }

  /** ground (X, Y) metres -> reference-frame pixel */
  function groundToRef(X, Y) {
    return dehom(mat3mulv(G, [X, Y, 1]));
  }
  /** reference pixel -> ground (X, Y), track.py's Ground.to_ground, for round-trip checks */
  function refToGround(u, v) {
    const yc = (v - cy) / f, xc = (u - cx) / f;
    const den = sp + yc * cp;
    if (!(den > 1e-3)) return null;
    const t = h / den;
    return { X: t * xc, Y: t * (cp - yc * sp) };
  }
  /** ENU -> ground model */
  function enToGround(e, n) {
    return { X: e + ox, Y: n + oy };
  }
  function groundToEN(X, Y) {
    return { e: X - ox, n: Y - oy };
  }
  function frameOf(clipTimeS) {
    return f0 + clipTimeS * fps;
  }
  /** ENU (e, n) at clip time -> pixel in the ORIGINAL clip (meta.width x meta.height), or null */
  function projectEN(e, n, clipTimeS) {
    const { X, Y } = enToGround(e, n);
    const ref = groundToRef(X, Y);
    if (!ref) return null;
    const Hi = hinvAt(frameOf(clipTimeS));
    if (!Hi) return ref;
    const out = dehom(mat3mulv(Hi, [ref.u, ref.v, 1]));
    return out;
  }
  /** pixel of the ORIGINAL clip at clip time -> ENU, or null (for click handling / checks) */
  function unprojectPixel(u, v, clipTimeS) {
    const Hi = hinvAt(frameOf(clipTimeS));
    let ru = u, rv = v;
    if (Hi) {
      const H = inv3(Hi);
      const r = dehom(mat3mulv(H, [u, v, 1]));
      if (!r) return null;
      ru = r.u; rv = r.v;
    }
    const gnd = refToGround(ru, rv);
    return gnd ? groundToEN(gnd.X, gnd.Y) : null;
  }
  return { meta, G, projectEN, unprojectPixel, groundToRef, refToGround, enToGround, groundToEN, hinvAt, frameOf, width: meta.width, height: meta.height };
}

/** Browser/Node loader: fetches /meta (or any URL) and builds the camera. */
export async function loadCamera(url = '/meta') {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`meta: ${r.status}`);
  return makeCamera(await r.json());
}
