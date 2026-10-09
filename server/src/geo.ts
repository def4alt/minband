// Geodetic anchor (S3): marker-frame metres <-> WGS84 lat/lon, UTM and MGRS.
//
// Marker frame (ios/MinBand/Origin.swift; PROTOCOL.md "metres, marker frame, Y up"): right-handed,
// origin at the marker centre, +Y up (gravity), +X along the printed image's width (to the right
// when read upright), +Z = X x Y toward the image's bottom edge. "Forward" is therefore -Z: from
// the marker centre toward the top edge of the image (manual "set origin here" fallback: the
// direction the phone faced). The anchor's `headingDeg` is the true bearing of -Z, so +X points
// to heading + 90 and, at heading 0, (x, -z, y) = (east, north, up).
//
// Marker -> ENU is a rotation about up. ENU -> WGS84 goes through ECEF, which is exact for a
// Cartesian local frame (what ARKit gives). The first-order meridian/prime-vertical radii formula
// (dlat = n / M, dlon = e / (N cos lat)) drifts from it by d^2 tan(lat) / 2N, 5 cm at 1 km and
// 1.3 m at 5 km at 34 degrees, because a straight line east leaves the parallel. Without an
// anchor altitude the anchor sits on the ellipsoid (h = 0): the horizontal error is d * dh / R,
// 0.08 m at 1 km for 500 m of wrong height.
//
// UTM: Krueger series to n^6 (Karney 2011, "Transverse Mercator with an accuracy of a few
// nanometers"), so the projection itself is exact at any sensible precision. MGRS: WGS84 "AA"
// lettering, Norway/Svalbard zone exceptions, coordinates truncated (not rounded) to the precision
// as GeographicLib and GEOTRANS do. Polar regions (UPS, lat < -80 or > 84) are not implemented.

const A = 6378137, F = 1 / 298.257223563, E2 = F * (2 - F), E = Math.sqrt(E2);
const K0 = 0.9996, FALSE_E = 500_000, FALSE_N_SOUTH = 10_000_000;
const D2R = Math.PI / 180, R2D = 180 / Math.PI;

export interface GeoAnchor {
  /** Marker origin, WGS84 degrees. */
  lat: number; lon: number;
  /** True bearing of the marker frame's -Z axis (toward the image's top edge), degrees. */
  headingDeg: number;
  /** Marker origin height above the WGS84 ellipsoid (m), null when unknown. */
  altM: number | null;
}
export interface LatLonAlt { lat: number; lon: number; alt: number }

/** `lat,lon,headingDeg[,altM]` (MINBAND_GEO). Throws with a readable message. */
export function parseAnchor(s: string): GeoAnchor {
  const p = s.split(',').map(x => x.trim());
  if (p.length < 3 || p.length > 4 || p.some(x => x === '')) throw new Error(`expected "lat,lon,headingDeg[,altM]", got "${s}"`);
  return checkAnchor({ lat: Number(p[0]), lon: Number(p[1]), headingDeg: Number(p[2]), altM: p.length === 4 ? Number(p[3]) : null });
}

/** Validates and normalises (heading to [0, 360), lon to [-180, 180)). */
export function checkAnchor(a: GeoAnchor): GeoAnchor {
  const errs: string[] = [];
  if (!Number.isFinite(a.lat) || Math.abs(a.lat) > 90) errs.push('lat must be in [-90, 90]');
  if (!Number.isFinite(a.lon) || Math.abs(a.lon) > 180) errs.push('lon must be in [-180, 180]');
  if (!Number.isFinite(a.headingDeg)) errs.push('heading must be a number (degrees true)');
  if (a.altM !== null && (!Number.isFinite(a.altM) || Math.abs(a.altM) > 20_000)) errs.push('alt must be in [-20000, 20000] m');
  if (errs.length) throw new Error(errs.join('; '));
  return { lat: a.lat, lon: wrapLon(a.lon), headingDeg: mod(a.headingDeg, 360), altM: a.altM };
}

const mod = (x: number, m: number) => ((x % m) + m) % m;
const wrapLon = (lon: number) => (lon >= -180 && lon < 180 ? lon : mod(lon + 180, 360) - 180);

// ---- marker frame <-> ENU -----------------------------------------------------------------------

/** Marker-frame vector -> [east, north, up]. Forward (-Z) maps to the heading. */
export function markerToEnu(p: readonly number[], headingDeg: number): [number, number, number] {
  const h = headingDeg * D2R, c = Math.cos(h), s = Math.sin(h);
  return [p[0] * c - p[2] * s, -p[0] * s - p[2] * c, p[1]];
}

/** [east, north, up] -> marker frame (the horizontal rotation is its own inverse). */
export function enuToMarker(enu: readonly number[], headingDeg: number): [number, number, number] {
  const h = headingDeg * D2R, c = Math.cos(h), s = Math.sin(h);
  return [enu[0] * c - enu[1] * s, enu[2], -enu[0] * s - enu[1] * c];
}

/** Marker-frame velocity -> course (degrees true, [0, 360)) and horizontal speed (m/s). */
export function courseSpeed(vel: readonly number[], headingDeg: number): { course: number; speed: number } {
  const [ve, vn] = markerToEnu(vel, headingDeg);
  const speed = Math.hypot(ve, vn);
  return { course: speed > 0 ? mod(Math.atan2(ve, vn) * R2D, 360) : 0, speed };
}

// ---- ENU <-> geodetic via ECEF ------------------------------------------------------------------

function toEcef(lat: number, lon: number, h: number): [number, number, number] {
  const p = lat * D2R, l = lon * D2R, sp = Math.sin(p), cp = Math.cos(p);
  const n = A / Math.sqrt(1 - E2 * sp * sp);
  return [(n + h) * cp * Math.cos(l), (n + h) * cp * Math.sin(l), (n * (1 - E2) + h) * sp];
}

function fromEcef(x: number, y: number, z: number): LatLonAlt {
  const p = Math.hypot(x, y);
  let lat = Math.atan2(z, p * (1 - E2)), h = 0;
  for (let i = 0; i < 6; i++) { // converges to < 1e-12 rad in 3 steps near the surface
    const s = Math.sin(lat), n = A / Math.sqrt(1 - E2 * s * s);
    h = p * Math.cos(lat) + z * s - A * A / n; // stable at the poles, unlike p / cos(lat) - n
    const next = Math.atan2(z, p * (1 - E2 * n / (n + h)));
    if (Math.abs(next - lat) < 1e-14) { lat = next; break; }
    lat = next;
  }
  return { lat: lat * R2D, lon: Math.atan2(y, x) * R2D, alt: h };
}

/** Local ENU offset (m) from the anchor -> WGS84. */
export function enuToGeodetic(enu: readonly number[], lat0: number, lon0: number, h0 = 0): LatLonAlt {
  const p = lat0 * D2R, l = lon0 * D2R, sp = Math.sin(p), cp = Math.cos(p), sl = Math.sin(l), cl = Math.cos(l);
  const [e, n, u] = enu, o = toEcef(lat0, lon0, h0);
  return fromEcef(
    o[0] - sl * e - sp * cl * n + cp * cl * u,
    o[1] + cl * e - sp * sl * n + cp * sl * u,
    o[2] + cp * n + sp * u,
  );
}

/** WGS84 -> local ENU offset (m) from the anchor. */
export function geodeticToEnu(lat: number, lon: number, h: number, lat0: number, lon0: number, h0 = 0): [number, number, number] {
  const p = lat0 * D2R, l = lon0 * D2R, sp = Math.sin(p), cp = Math.cos(p), sl = Math.sin(l), cl = Math.cos(l);
  const a = toEcef(lat, lon, h), o = toEcef(lat0, lon0, h0);
  const dx = a[0] - o[0], dy = a[1] - o[1], dz = a[2] - o[2];
  return [-sl * dx + cl * dy, -sp * cl * dx - sp * sl * dy + cp * dz, cp * cl * dx + cp * sl * dy + sp * dz];
}

/** Marker-frame position -> WGS84. `alt` is meaningful only when the anchor has `altM`. */
export function markerToGeodetic(pos: readonly number[], a: GeoAnchor): LatLonAlt {
  return enuToGeodetic(markerToEnu(pos, a.headingDeg), a.lat, a.lon, a.altM ?? 0);
}

/** WGS84 -> marker frame (h defaults to the anchor height, i.e. y ~ 0 near the marker). */
export function geodeticToMarker(lat: number, lon: number, a: GeoAnchor, h = a.altM ?? 0): [number, number, number] {
  return enuToMarker(geodeticToEnu(lat, lon, h, a.lat, a.lon, a.altM ?? 0), a.headingDeg);
}

// ---- UTM (Krueger n^6) --------------------------------------------------------------------------

const N3 = F / (2 - F);
const TM_A = A / (1 + N3) * (1 + N3 ** 2 / 4 + N3 ** 4 / 64 + N3 ** 6 / 256);
const poly = (c: number[]) => c.reduce((s, k, i) => s + k * N3 ** (i + 1), 0);
// Karney (2011) eq. 35 (alpha, forward) and 36 (beta, reverse); coefficient i is for n^(i+1).
const ALPHA = [
  poly([1 / 2, -2 / 3, 5 / 16, 41 / 180, -127 / 288, 7891 / 37800]),
  poly([0, 13 / 48, -3 / 5, 557 / 1440, 281 / 630, -1983433 / 1935360]),
  poly([0, 0, 61 / 240, -103 / 140, 15061 / 26880, 167603 / 181440]),
  poly([0, 0, 0, 49561 / 161280, -179 / 168, 6601661 / 7257600]),
  poly([0, 0, 0, 0, 34729 / 80640, -3418889 / 1995840]),
  poly([0, 0, 0, 0, 0, 212378941 / 319334400]),
];
const BETA = [
  poly([1 / 2, -2 / 3, 37 / 96, -1 / 360, -81 / 512, 96199 / 604800]),
  poly([0, 1 / 48, 1 / 15, -437 / 1440, 46 / 105, -1118711 / 3870720]),
  poly([0, 0, 17 / 480, -37 / 840, -209 / 4480, 5569 / 90720]),
  poly([0, 0, 0, 4397 / 161280, -11 / 504, -830251 / 7257600]),
  poly([0, 0, 0, 0, 4583 / 161280, -108847 / 3991680]),
  poly([0, 0, 0, 0, 0, 20648693 / 638668800]),
];

export interface Utm { zone: number; north: boolean; easting: number; northing: number }

/** Standard UTM zone with the Norway (32V) and Svalbard (31X/33X/35X/37X) exceptions. */
export function utmZone(lat: number, lon: number): number {
  const l = wrapLon(lon);
  let z = Math.floor((l + 180) / 6) + 1;
  if (lat >= 56 && lat < 64 && l >= 3 && l < 12) z = 32;
  if (lat >= 72 && lat <= 84 && l >= 0 && l < 42) z = l < 9 ? 31 : l < 21 ? 33 : l < 33 ? 35 : 37;
  return z;
}

/** WGS84 -> UTM (zone forced with `zone`, e.g. to stay in a neighbour zone). */
export function toUtm(lat: number, lon: number, zone = utmZone(lat, lon)): Utm {
  if (!(lat >= -80 && lat <= 84)) throw new RangeError(`latitude ${lat} outside UTM (-80..84); UPS not implemented`);
  const phi = lat * D2R, lam = wrapLon(lon - (zone * 6 - 183)) * D2R;
  const tau = Math.tan(phi);
  const sigma = Math.sinh(E * Math.atanh(E * tau / Math.sqrt(1 + tau * tau)));
  const tauP = tau * Math.sqrt(1 + sigma * sigma) - sigma * Math.sqrt(1 + tau * tau);
  const xiP = Math.atan2(tauP, Math.cos(lam)), etaP = Math.asinh(Math.sin(lam) / Math.sqrt(tauP * tauP + Math.cos(lam) ** 2));
  let xi = xiP, eta = etaP;
  for (let j = 1; j <= 6; j++) {
    xi += ALPHA[j - 1] * Math.sin(2 * j * xiP) * Math.cosh(2 * j * etaP);
    eta += ALPHA[j - 1] * Math.cos(2 * j * xiP) * Math.sinh(2 * j * etaP);
  }
  const north = lat >= 0;
  return { zone, north, easting: FALSE_E + K0 * TM_A * eta, northing: K0 * TM_A * xi + (north ? 0 : FALSE_N_SOUTH) };
}

/** UTM -> WGS84 degrees. */
export function fromUtm(u: Utm): { lat: number; lon: number } {
  const xi = (u.northing - (u.north ? 0 : FALSE_N_SOUTH)) / (K0 * TM_A), eta = (u.easting - FALSE_E) / (K0 * TM_A);
  let xiP = xi, etaP = eta;
  for (let j = 1; j <= 6; j++) {
    xiP -= BETA[j - 1] * Math.sin(2 * j * xi) * Math.cosh(2 * j * eta);
    etaP -= BETA[j - 1] * Math.cos(2 * j * xi) * Math.sinh(2 * j * eta);
  }
  const tauP = Math.sin(xiP) / Math.sqrt(Math.sinh(etaP) ** 2 + Math.cos(xiP) ** 2);
  let tau = tauP;
  for (let i = 0; i < 10; i++) { // Newton on tau' (Karney eq. 19-21)
    const sigma = Math.sinh(E * Math.atanh(E * tau / Math.sqrt(1 + tau * tau)));
    const tauI = tau * Math.sqrt(1 + sigma * sigma) - sigma * Math.sqrt(1 + tau * tau);
    const d = (tauP - tauI) / Math.sqrt(1 + tauI * tauI) * (1 + (1 - E2) * tau * tau) / ((1 - E2) * Math.sqrt(1 + tau * tau));
    tau += d;
    if (Math.abs(d) < 1e-14) break;
  }
  return { lat: Math.atan(tau) * R2D, lon: wrapLon(u.zone * 6 - 183 + Math.atan2(Math.sinh(etaP), Math.cos(xiP)) * R2D) };
}

// ---- MGRS ---------------------------------------------------------------------------------------

const BANDS = 'CDEFGHJKLMNPQRSTUVWX'; // 8 degrees from -80, X is 72..84
const COLS = ['ABCDEFGH', 'JKLMNPQR', 'STUVWXYZ']; // set by (zone - 1) % 3
const ROWS = 'ABCDEFGHJKLMNPQRSTUV'; // repeats every 2000 km; even zones start 5 letters in

export function latBand(lat: number): string {
  if (!(lat >= -80 && lat <= 84)) throw new RangeError(`latitude ${lat} outside MGRS UTM bands (-80..84); UPS not implemented`);
  return BANDS[Math.min(19, Math.floor((lat + 80) / 8))];
}

/**
 * WGS84 -> MGRS, e.g. `52S CG 21424 59640`, `05Q KB 12345 67890`. `precisionM` is 1, 10, ..., 100000;
 * digits are truncated, so the reference names the square's south-west corner.
 */
export function toMgrs(lat: number, lon: number, precisionM = 1): string {
  const digits = 5 - Math.round(Math.log10(precisionM));
  if (!(digits >= 0 && digits <= 5) || 10 ** (5 - digits) !== precisionM) throw new RangeError('precisionM must be 1, 10, ..., 100000');
  const band = latBand(lat), u = toUtm(lat, lon);
  const e100 = Math.floor(u.easting / 1e5), n100 = Math.floor(u.northing / 1e5);
  const sq = COLS[(u.zone - 1) % 3][e100 - 1] + ROWS[(n100 + (u.zone % 2 === 0 ? 5 : 0)) % 20];
  const div = 10 ** (5 - digits);
  const fmt = (v: number) => String(Math.floor((v - Math.floor(v / 1e5) * 1e5) / div)).padStart(digits, '0');
  const gzd = String(u.zone).padStart(2, '0') + band; // 2-digit zone like GEOTRANS and GeographicLib
  return digits ? `${gzd} ${sq} ${fmt(u.easting)} ${fmt(u.northing)}` : `${gzd} ${sq}`;
}

/** MGRS (spaces optional) -> UTM of the square's centre, plus its precision in metres. */
export function mgrsToUtm(s: string): Utm & { precisionM: number } {
  const m = /^(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z])([A-HJ-NP-V])(\d{0,10})$/.exec(s.replace(/\s+/g, '').toUpperCase());
  if (!m || m[5].length % 2) throw new Error(`not an MGRS reference: "${s}"`);
  const zone = Number(m[1]), band = m[2], digits = m[5].length / 2;
  if (zone < 1 || zone > 60) throw new Error(`MGRS zone ${zone} outside 1..60`);
  const col = COLS[(zone - 1) % 3].indexOf(m[3]);
  if (col < 0) throw new Error(`column letter ${m[3]} is not used in zone ${zone}`);
  const row = mod(ROWS.indexOf(m[4]) - (zone % 2 === 0 ? 5 : 0), 20);
  const precisionM = 10 ** (5 - digits), half = precisionM / 2;
  const easting = (col + 1) * 1e5 + (digits ? Number(m[5].slice(0, digits)) * precisionM : 0) + half;
  let northing = row * 1e5 + (digits ? Number(m[5].slice(digits)) * precisionM : 0) + half;
  // Rows repeat every 2000 km: take the first repeat at or above the band's southern edge (on the
  // central meridian, less 100 km for the parallels' curvature across the zone).
  const north = band >= 'N', bandLat = -80 + BANDS.indexOf(band) * 8;
  const minN = toUtm(bandLat, zone * 6 - 183, zone).northing - 1e5;
  while (northing < minN) northing += 2e6;
  return { zone, north, easting, northing, precisionM };
}

/** MGRS -> WGS84 degrees of the square's centre. */
export function fromMgrs(s: string): { lat: number; lon: number; precisionM: number } {
  const u = mgrsToUtm(s);
  return { ...fromUtm(u), precisionM: u.precisionM };
}

/** MGRS at 1 m, '' in the polar caps (UPS is not implemented). */
export const mgrsOrEmpty = (lat: number, lon: number) => (lat >= -80 && lat <= 84 ? toMgrs(lat, lon) : '');

/** `{lat, lon, mgrs}` of a marker-frame position (GlobalEntity.geo). */
export function geoPoint(pos: readonly number[], a: GeoAnchor): { lat: number; lon: number; mgrs: string } {
  const g = markerToGeodetic(pos, a);
  return { lat: g.lat, lon: g.lon, mgrs: mgrsOrEmpty(g.lat, g.lon) };
}

/**
 * GET /api/geo parameters -> new anchor. `lat`, `lon` (or `mgrs`, centre of the square),
 * `heading`, `alt` (empty or "none" = unknown); fields not given keep the current anchor's, so
 * `?heading=93` alone corrects the heading on stage. `clear=1` removes the anchor.
 */
export function anchorFromQuery(cur: GeoAnchor | null, q: Record<string, string>): GeoAnchor | null {
  const unknown = Object.keys(q).filter(k => !['lat', 'lon', 'heading', 'alt', 'mgrs', 'clear'].includes(k));
  if (unknown.length) throw new Error(`unknown parameter(s): ${unknown.join(', ')}`);
  if (q.clear !== undefined) {
    if (!['1', 'true'].includes(q.clear) || Object.keys(q).length > 1) throw new Error('clear=1 takes no other parameters');
    return null;
  }
  if (!Object.keys(q).length) return cur;
  if (q.mgrs !== undefined && (q.lat !== undefined || q.lon !== undefined)) throw new Error('give lat/lon or mgrs, not both');
  const num = (k: string) => (q[k] === undefined ? undefined : q[k].trim() === '' ? NaN : Number(q[k]));
  let lat = num('lat') ?? cur?.lat, lon = num('lon') ?? cur?.lon;
  if (q.mgrs !== undefined) ({ lat, lon } = fromMgrs(q.mgrs));
  const headingDeg = num('heading') ?? cur?.headingDeg;
  const altM = q.alt === undefined ? cur?.altM ?? null : ['', 'none', 'null'].includes(q.alt.trim()) ? null : Number(q.alt);
  if (lat === undefined || lon === undefined || headingDeg === undefined) throw new Error('lat, lon (or mgrs) and heading are required to set an anchor');
  return checkAnchor({ lat, lon, headingDeg, altM });
}

/** GET /api/geo response body. */
export function anchorInfo(a: GeoAnchor | null) {
  return { anchor: a && { lat: a.lat, lon: a.lon, headingDeg: a.headingDeg, altM: a.altM, mgrs: mgrsOrEmpty(a.lat, a.lon) } };
}

/** Snapshot.geo: the anchor plus its grid reference, null when not configured. */
export function anchorView(a: GeoAnchor | null): { lat: number; lon: number; headingDeg: number; mgrs: string } | null {
  return a && { lat: a.lat, lon: a.lon, headingDeg: a.headingDeg, mgrs: mgrsOrEmpty(a.lat, a.lon) };
}
