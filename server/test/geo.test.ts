import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  anchorFromQuery, courseSpeed, enuToGeodetic, enuToMarker, fromMgrs, fromUtm, geodeticToEnu, geodeticToMarker,
  markerToEnu, markerToGeodetic, mgrsToUtm, parseAnchor, toMgrs, toUtm, utmZone, type GeoAnchor,
} from '../src/geo.js';

// Reference vectors. MGRS (1 m) from NGA GEOTRANS (C code, via PyPI `mgrs` 1.5.4), identical in
// PyGeodesy 26.9.9; UTM easting/northing from PROJ 9.8.1 (pyproj 3.8.0, `+proj=utm +ellps=WGS84`).
// Offline, geo.ts also matched both on 26,000 random and edge-case points: every MGRS string
// equal to PyGeodesy's and to GEOTRANS's except 5 points within 0.5 mm of a metre line (GEOTRANS
// truncates its own slightly different projection there), UTM within 2e-8 m of PROJ.
const VECTORS: { name: string; lat: number; lon: number; mgrs: string; zone: number; e: number; n: number }[] = [
  { name: 'Seoul City Hall', lat: 37.5665, lon: 126.978, mgrs: '52S CG 21424 59640', zone: 52, e: 321424.286, n: 4159640.641 },
  { name: 'Pyongyang (odd zone)', lat: 39.0392, lon: 125.7625, mgrs: '51S YD 39097 24758', zone: 51, e: 739097.007, n: 4324758.694 },
  { name: 'Washington Monument', lat: 38.8895, lon: -77.0352, mgrs: '18S UJ 23486 06483', zone: 18, e: 323486.737, n: 4306483.048 },
  { name: 'Null Island', lat: 0, lon: 0, mgrs: '31N AA 66021 00000', zone: 31, e: 166021.443, n: 0 },
  { name: 'just south of the equator', lat: -0.000001, lon: 10, mgrs: '32M PE 11280 99999', zone: 32, e: 611280.651, n: 9999999.889 },
  { name: 'Sydney Opera House', lat: -33.8568, lon: 151.2153, mgrs: '56H LH 34900 52288', zone: 56, e: 334900.570, n: 6252288.753 },
  { name: 'Buenos Aires', lat: -34.6037, lon: -58.3816, mgrs: '21H UB 73317 70036', zone: 21, e: 373317.502, n: 6170036.171 },
  { name: 'southern limit', lat: -79.9, lon: -60, mgrs: '21C VM 41292 28062', zone: 21, e: 441292.553, n: 1128062.171 },
  { name: 'zone edge, west side', lat: 45, lon: 5.999999, mgrs: '31T GK 36445 87329', zone: 31, e: 736445.947, n: 4987329.502 },
  { name: 'zone edge, east side', lat: 45, lon: 6, mgrs: '32T KQ 63553 87329', zone: 32, e: 263553.974, n: 4987329.505 },
  { name: 'Bergen (Norway exception)', lat: 60.3913, lon: 5.3221, mgrs: '32V KN 97353 00648', zone: 32, e: 297353.933, n: 6700648.345 },
  { name: '3.5E 60N is 32V, not 31V', lat: 60, lon: 3.5, mgrs: '32V JM 93458 64167', zone: 32, e: 193458.670, n: 6664167.679 },
  { name: 'Longyearbyen (Svalbard 33X)', lat: 78.2232, lon: 15.6267, mgrs: '33X WG 14278 83355', zone: 33, e: 514278.715, n: 8683355.469 },
  { name: 'Svalbard 31X', lat: 78, lon: 8.9, mgrs: '31X FG 36716 65261', zone: 31, e: 636716.846, n: 8665261.550 },
  { name: 'Svalbard 33X (no 32X)', lat: 78, lon: 9.1, mgrs: '33X UG 63283 65261', zone: 33, e: 363283.154, n: 8665261.550 },
  { name: 'band X top', lat: 83.9, lon: 20, mgrs: '33X WP 59245 19502', zone: 33, e: 559245.722, n: 9319502.269 },
];

test('UTM and MGRS match GEOTRANS and PROJ reference vectors', () => {
  for (const v of VECTORS) {
    assert.equal(toMgrs(v.lat, v.lon), v.mgrs, v.name);
    assert.equal(utmZone(v.lat, v.lon), v.zone, v.name);
    const u = toUtm(v.lat, v.lon);
    assert.equal(u.north, v.lat >= 0, v.name);
    assert.ok(Math.abs(u.easting - v.e) < 0.001 && Math.abs(u.northing - v.n) < 0.001, `${v.name}: ${u.easting} ${u.northing}`);
    const back = fromUtm(u);
    assert.ok(Math.abs(back.lat - v.lat) < 1e-9 && Math.abs(back.lon - v.lon) < 1e-9, `${v.name} inverse`);
  }
});

test('MGRS precision, truncation and the GeographicLib published example', () => {
  // GeoConvert(1) man page: "echo E44d24 N33d20 | GeoConvert -m -p -3 => 38SMB4488", and
  // "echo 38SMB4488 | GeoConvert => 33.33424 44.40363" (input = centre of the 1 km square).
  assert.equal(toMgrs(33 + 20 / 60, 44.4, 1000), '38S MB 44 88');
  const c = fromMgrs('38SMB4488');
  assert.equal(c.precisionM, 1000);
  assert.equal(c.lat.toFixed(5), '33.33424');
  assert.equal(c.lon.toFixed(5), '44.40363');
  // Truncation, not rounding: 321424.286 E, 4159640.641 N.
  assert.equal(toMgrs(37.5665, 126.978, 10), '52S CG 2142 5964');
  assert.equal(toMgrs(37.5665, 126.978, 100000), '52S CG');
  assert.equal(toMgrs(-33.8568, 151.2153, 100), '56H LH 349 522');
  assert.throws(() => toMgrs(37, 127, 5), /precisionM/);
  assert.throws(() => toMgrs(85, 0), /UPS/);
  assert.throws(() => toMgrs(-80.5, 0), /UPS/);
});

test('MGRS parse round trip, including the 2000 km row cycle in both hemispheres', () => {
  for (const v of VECTORS) {
    const u = mgrsToUtm(v.mgrs);
    assert.equal(u.zone, v.zone, v.name);
    assert.ok(Math.abs(u.easting - (Math.floor(v.e) + 0.5)) < 1e-6 && Math.abs(u.northing - (Math.floor(v.n) + 0.5)) < 1e-6, `${v.name}: ${u.easting} ${u.northing}`);
    // Centre of the square back to UTM in the same zone (a point on a zone edge has its centre
    // in either zone, so compare in the zone the reference names).
    const ll = fromMgrs(v.mgrs.replace(/ /g, '').toLowerCase()), u2 = toUtm(ll.lat, ll.lon, v.zone);
    assert.ok(Math.abs(u2.easting - u.easting) < 1e-6 && Math.abs(u2.northing - u.northing) < 1e-6, `${v.name} inverse`);
    if (!/edge|limit/.test(v.name)) assert.equal(toMgrs(ll.lat, ll.lon), v.mgrs, `${v.name} re-encode`);
  }
  assert.equal(mgrsToUtm('4QFJ1234567890').zone, 4); // 1-digit zones are accepted
  assert.throws(() => fromMgrs('52S CI 1 2'), /MGRS/); // I is never a letter, odd digit count
  assert.throws(() => fromMgrs('52S JG 12345 67890'), /column letter J/); // zone 52 uses A..H
});

test('marker frame -> ENU: -Z is forward at the heading, +X to its right', () => {
  const close = (a: number[], b: number[]) => a.every((x, i) => Math.abs(x - b[i]) < 1e-9);
  assert.ok(close(markerToEnu([0, 0, -10], 0), [0, 10, 0]), 'heading 0: forward is north');
  assert.ok(close(markerToEnu([0, 0, -10], 90), [10, 0, 0]), 'heading 90: forward is east');
  assert.ok(close(markerToEnu([10, 0, 0], 90), [0, -10, 0]), 'heading 90: +X is south');
  assert.ok(close(markerToEnu([0, 2, 0], 123), [0, 0, 2]), 'Y is up');
  for (const h of [0, 37, 90, 181, 359.5]) {
    const p = [3.1, -0.4, 7.7];
    assert.ok(close(enuToMarker(markerToEnu(p, h), h), p), `inverse at ${h}`);
  }
  const cs = courseSpeed([0, 0, -1.5], 90);
  assert.ok(Math.abs(cs.course - 90) < 1e-9 && Math.abs(cs.speed - 1.5) < 1e-12);
  assert.ok(Math.abs(courseSpeed([1, 0, 0], 0).course - 90) < 1e-9);
  assert.ok(Math.abs(courseSpeed([0, 0, 1], 30).course - 210) < 1e-9);
  assert.deepEqual(courseSpeed([0, 0, 0], 30), { course: 0, speed: 0 });
});

test('an entity 10 m along the forward axis with heading 90 ends up 10 m east', () => {
  const a: GeoAnchor = { lat: 37.5665, lon: 126.978, headingDeg: 90, altM: 38 };
  const g = markerToGeodetic([0, 0, -10], a);
  const enu = geodeticToEnu(g.lat, g.lon, g.alt, a.lat, a.lon, 38);
  assert.ok(Math.abs(enu[0] - 10) < 1e-6 && Math.abs(enu[1]) < 1e-6 && Math.abs(enu[2]) < 1e-6, enu.join());
  // Independent check with WGS84 radii: dlon = E / (N cos lat).
  const s = Math.sin(a.lat * Math.PI / 180), e2 = 0.0066943799901413165;
  const nRad = 6378137 / Math.sqrt(1 - e2 * s * s);
  assert.ok(Math.abs((g.lon - a.lon) - 10 / ((nRad + 38) * Math.cos(a.lat * Math.PI / 180)) * 180 / Math.PI) < 1e-10);
  assert.ok(Math.abs(g.lat - a.lat) < 1e-10);
  assert.ok(Math.abs(g.alt - 38) < 1e-5, 'ground stays at the anchor height over 10 m');
});

test('ENU via ECEF matches the WGS84 radii up to the parallel curvature term, and round-trips', () => {
  const a: GeoAnchor = { lat: -33.8568, lon: 151.2153, headingDeg: 211, altM: 5 };
  const phi = a.lat * Math.PI / 180, sl = Math.sin(phi), e2 = 0.0066943799901413165, w = Math.sqrt(1 - e2 * sl * sl);
  const m = 6378137 * (1 - e2) / w ** 3, nr = 6378137 / w;
  for (const [e, n] of [[1000, 0], [0, 1000], [0, -2000]]) {
    const g = enuToGeodetic([e, n, 0], a.lat, a.lon, 5);
    const dn = (g.lat - a.lat) * Math.PI / 180 * (m + 5), de = (g.lon - a.lon) * Math.PI / 180 * (nr + 5) * Math.cos(phi);
    // A straight line east leaves the parallel toward the equator by e^2 tan(lat) / 2N (5 cm here).
    const curl = -e * e * Math.tan(phi) / (2 * nr);
    assert.ok(Math.hypot(dn - n - curl, de - e) < 0.005, `${e},${n}: ${dn} ${de} (curl ${curl})`);
    assert.ok(Math.abs(g.alt - 5 - (e * e + n * n) / (2 * 6371000)) < 0.01, 'a tangent plane rises above the ellipsoid by d^2/2R');
  }
  for (const p of [[0, 0, 0], [12.5, 1.7, -40], [-2500, 30, 1800]]) {
    const g = markerToGeodetic(p, a);
    const back = geodeticToMarker(g.lat, g.lon, a, g.alt);
    assert.ok(Math.hypot(back[0] - p[0], back[1] - p[1], back[2] - p[2]) < 1e-6, `${p}: ${back}`);
  }
});

test('anchor parsing (MINBAND_GEO) and /api/geo query rules', () => {
  assert.deepEqual(parseAnchor('37.5665, 126.978, 90'), { lat: 37.5665, lon: 126.978, headingDeg: 90, altM: null });
  assert.deepEqual(parseAnchor('37.5,127,-10,42.5'), { lat: 37.5, lon: 127, headingDeg: 350, altM: 42.5 });
  assert.equal(parseAnchor('0,180,0').lon, -180);
  for (const bad of ['37.5,127', '37.5,127,x', '95,127,0', '37,190,0', '37,127,0,1,2', '37,,0']) assert.throws(() => parseAnchor(bad), Error, bad);

  const a = anchorFromQuery(null, { lat: '37.5665', lon: '126.978', heading: '90', alt: '38' })!;
  assert.deepEqual(a, { lat: 37.5665, lon: 126.978, headingDeg: 90, altM: 38 });
  assert.equal(anchorFromQuery(a, {}), a, 'no parameters: read only');
  assert.deepEqual(anchorFromQuery(a, { heading: '93.5' }), { ...a, headingDeg: 93.5 }, 'partial update keeps the rest');
  assert.deepEqual(anchorFromQuery(a, { alt: '' }), { ...a, altM: null });
  const m = anchorFromQuery(null, { mgrs: '52S CG 21424 59640', heading: '0' })!;
  assert.ok(Math.abs(m.lat - 37.5665) < 1e-5 && Math.abs(m.lon - 126.978) < 1e-5);
  assert.equal(anchorFromQuery(a, { clear: '1' }), null);
  assert.throws(() => anchorFromQuery(null, { lat: '37', lon: '127' }), /required/);
  assert.throws(() => anchorFromQuery(a, { lat: 'north' }), /lat/);
  assert.throws(() => anchorFromQuery(a, { heading: '' }), /heading/);
  assert.throws(() => anchorFromQuery(a, { mgrs: '52SCG', lat: '1' }), /not both/);
  assert.throws(() => anchorFromQuery(a, { clear: '1', lat: '1' }), /clear/);
  assert.throws(() => anchorFromQuery(a, { latitude: '1' }), /unknown/);
});
