// Gear Generator — geometry core (no DOM, runs in Node too).
//
// Involute gears made the way a gear is cut: a basic rack (with its rounded
// tip) is rolled around the blank, and the tooth is the envelope it leaves.
// That gives the true trochoidal root fillet and correct undercut on small
// tooth counts. Supports profile shift, backlash, spur / helical / herringbone
// teeth, a mating gear or rack, bores (round, D, hex, keyway), a hub, and
// filament-saving cut-outs (spokes, round holes, radial slots).
//
// Cap triangulation (polygons with holes) is passed in by the caller as
// triangulate(contour, holes) with [x, y] points, returning index triples, e.g. a
// wrapper around THREE.ShapeUtils.triangulateShape. So this file needs nothing
// beyond ../lib/print3d.js. All lengths are millimetres, angles in degrees at the API.

import { Mesh, stlBinary, PRINTERS, plateOrigin } from '../lib/print3d.js';
export { PRINTERS, stlBinary };

const DEG = Math.PI / 180;
const C_STAR = 0.25;     // bottom clearance coefficient (dedendum = 1.25 m)
const RHO_STAR = 0.38;   // rack tip / root fillet radius coefficient
const FIT = 0.15;        // default extra diameter on bores so printed parts slide on

const rot = ([x, y], a) => { const c = Math.cos(a), s = Math.sin(a); return [x * c - y * s, x * s + y * c]; };
const inv = a => Math.tan(a) - a;
function invInverse(v) {                     // solve tan(a) - a = v
  let a = Math.max(1e-4, Math.cbrt(3 * v));
  for (let i = 0; i < 50; i++) { const t = Math.tan(a); a -= (t - a - v) / (t * t); }
  return a;
}
function segInt(p1, p2, p3, p4) {            // proper intersection point of two segments, or null
  const d = (p2[0] - p1[0]) * (p4[1] - p3[1]) - (p2[1] - p1[1]) * (p4[0] - p3[0]);
  if (Math.abs(d) < 1e-14) return null;
  const t = ((p3[0] - p1[0]) * (p4[1] - p3[1]) - (p3[1] - p1[1]) * (p4[0] - p3[0])) / d;
  const u = ((p3[0] - p1[0]) * (p2[1] - p1[1]) - (p3[1] - p1[1]) * (p2[0] - p1[0])) / d;
  if (t <= 1e-9 || t >= 1 - 1e-9 || u <= 1e-9 || u >= 1 - 1e-9) return null;
  return [p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1])];
}
function circleSegInt(a, b, R) {             // point on segment a→b at radius R (a outside, b inside)
  let lo = 0, hi = 1;
  for (let i = 0; i < 60; i++) {
    const m = (lo + hi) / 2, x = a[0] + (b[0] - a[0]) * m, y = a[1] + (b[1] - a[1]) * m;
    if (Math.hypot(x, y) > R) lo = m; else hi = m;
  }
  return [a[0] + (b[0] - a[0]) * hi, a[1] + (b[1] - a[1]) * hi];
}
const cw = p => Math.atan2(p[0], p[1]);      // clockwise angle from +y
const polar = (R, a) => [R * Math.sin(a), R * Math.cos(a)];   // inverse of cw()
function dedupe(pts, eps = 1e-6) {
  const out = [];
  for (const p of pts) { const q = out[out.length - 1]; if (!q || Math.hypot(p[0] - q[0], p[1] - q[1]) > eps) out.push(p); }
  const f = out[0], l = out[out.length - 1];
  if (out.length > 1 && Math.hypot(f[0] - l[0], f[1] - l[1]) <= eps) out.pop();
  return out;
}
// Douglas–Peucker: drop points within tol of the chord (keeps both ends).
function simplify(pts, tol) {
  if (pts.length < 3) return pts;
  const keep = new Uint8Array(pts.length); keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop(), [ax, ay] = pts[a], [bx, by] = pts[b];
    const L = Math.hypot(bx - ax, by - ay) || 1e-12;
    let best = -1, bi = -1;
    for (let i = a + 1; i < b; i++) {
      const d = Math.abs((bx - ax) * (ay - pts[i][1]) - (ax - pts[i][0]) * (by - ay)) / L;
      if (d > best) { best = d; bi = i; }
    }
    if (best > tol) { keep[bi] = 1; stack.push([a, bi], [bi, b]); }
  }
  return pts.filter((_, i) => keep[i]);
}
function area(p) { let a = 0; for (let i = 0; i < p.length; i++) { const q = p[(i + 1) % p.length]; a += p[i][0] * q[1] - q[0] * p[i][1]; } return a / 2; }
const ccw = p => (area(p) < 0 ? p.slice().reverse() : p);
const cwise = p => (area(p) > 0 ? p.slice().reverse() : p);
function circle(R, n = 64, cx = 0, cy = 0) { const p = []; for (let i = 0; i < n; i++) { const a = (i / n) * 2 * Math.PI; p.push([cx + R * Math.cos(a), cy + R * Math.sin(a)]); } return p; }
function arc(cx, cy, R, a0, a1, n) { const p = []; for (let i = 0; i <= n; i++) { const a = a0 + (a1 - a0) * i / n; p.push([cx + R * Math.cos(a), cy + R * Math.sin(a)]); } return p; }

// ───────────────────────────── tooth geometry ─────────────────────────────
// Transverse basics for a gear of z teeth and profile shift x.
export function basics(z, x, g) {
  const mt = g.mn / Math.cos(g.beta);
  const at = Math.atan(Math.tan(g.alpha) / Math.cos(g.beta));
  const r = mt * z / 2, rb = r * Math.cos(at);
  let ra = r + (1 + x) * g.mn, topped = 0;
  // Keep a flat on every tooth tip (at least 0.25 m): trim the tip circle where
  // profile shift or few teeth would otherwise bring the tooth to a point.
  const s = Math.PI * mt / 2 + 2 * x * g.mn * Math.tan(at) - g.backlash / 2;
  const thick = R => 2 * R * (s / (2 * r) + inv(at) - inv(Math.acos(Math.min(1, rb / R))));
  const minTip = 0.25 * g.mn;
  if (ra > rb && thick(ra) < minTip) {
    let lo = Math.max(rb, r - g.mn), hi = ra;
    for (let i = 0; i < 60; i++) { const mid = (lo + hi) / 2; if (thick(mid) < minTip) hi = mid; else lo = mid; }
    topped = ra - lo; ra = lo;
  }
  return { z, x, mt, at, r, rb, ra, rf: r - (1 + C_STAR - x) * g.mn, topped };
}

// Smallest profile shift that avoids undercut.
export const minShift = (z, g) => {
  const at = Math.atan(Math.tan(g.alpha) / Math.cos(g.beta));
  return 1 - (z * Math.sin(at) ** 2) / (2 * Math.cos(g.beta));
};

// One tooth (CCW), centred on +y, from the middle of the space on its right to
// the middle of the space on its left. Built by enveloping the basic rack.
function tooth(b, g) {
  const { z, x, mt, at, r, ra, rf } = b, mn = g.mn;
  const yref = r + x * mn, yt = yref - (1 + C_STAR) * mn;            // rack reference and tip lines
  const tanA = Math.tan(at), sinA = Math.sin(at), cosA = Math.cos(at);
  const s0half = (Math.PI * mt / 2 - g.backlash / 2) / 2;            // half space width at the reference line
  const wtip = (Math.PI * mt / 2 + g.backlash / 2) - 2 * (1 + C_STAR) * mn * tanA;
  const rho = Math.max(0, Math.min(RHO_STAR * mn, (wtip / 2) * cosA / (1 - sinA) * 0.999));
  const yF = yt + rho * (1 - sinA);                                  // where the rack flank meets its tip fillet
  const xFlank = y => s0half + (yref - y) * tanA;
  const C = [xFlank(yF) + rho * cosA, yF + rho * sinA];
  // rack point Q with normal n touches the gear when n passes through the pitch point (0, r)
  const gen = (Q, nx, ny) => { const s = (Q[1] - r) * nx / ny - Q[0]; return rot([Q[0] + s, Q[1]], s / r); };

  let pts = [];
  const yTop = yref + 1.25 * mn, NF = 120;
  for (let i = 0; i <= NF; i++) { const y = yTop + (yF - yTop) * i / NF; pts.push(gen([xFlank(y), y], -cosA, -sinA)); }
  const nFlank = pts.length, NR = 160;
  for (let i = 1; i <= NR; i++) {
    const th = Math.PI + at + (Math.PI / 2 - at) * i / NR;
    pts.push(gen([C[0] + rho * Math.cos(th), C[1] + rho * Math.sin(th)], Math.cos(th), Math.sin(th)));
  }
  // Undercut: the fillet's trochoid cuts into the involute. Keep the outer envelope.
  let undercut = false;
  search: for (let i = 0; i < nFlank - 1; i++) {
    for (let j = nFlank; j < pts.length - 1; j++) {
      const hit = segInt(pts[i], pts[i + 1], pts[j], pts[j + 1]);
      if (hit) { pts.splice(i + 1, j - i, hit); undercut = true; break search; }
    }
  }
  // trim to the tip circle
  let k = 0;
  while (k < pts.length - 1 && Math.hypot(...pts[k + 1]) > ra) k++;
  pts = [circleSegInt(pts[k], pts[k + 1], ra), ...pts.slice(k + 1)];  // tip → root
  // pointed tooth: the flank reaches the centre line below the tip circle
  let pointed = false;
  for (let i = 0; i < pts.length - 1; i++) {
    if (pts[i][0] < 0 && pts[i + 1][0] >= 0) {
      const t = pts[i][0] / (pts[i][0] - pts[i + 1][0]);
      pts = [[0, pts[i][1] + t * (pts[i + 1][1] - pts[i][1])], ...pts.slice(i + 1)];
      pointed = true; break;
    }
  }
  // stop at the middle of the space (adjacent root fillets meet there on small gears)
  const half = Math.PI / z;
  let rootEnd = pts.length;
  for (let i = 1; i < pts.length; i++) {
    if (cw(pts[i]) > half) {
      const a = pts[i - 1], bb = pts[i], ta = cw(a), tb = cw(bb), t = (half - ta) / (tb - ta);
      pts = [...pts.slice(0, i), [a[0] + (bb[0] - a[0]) * t, a[1] + (bb[1] - a[1]) * t]];
      rootEnd = -1; break;
    }
  }
  const right = pts.slice().reverse();                                // root → tip, clockwise angle decreasing
  const out = [];
  if (rootEnd !== -1) {                                               // root arc from the space middle to the fillet end
    const aEnd = cw(right[0]), n = Math.max(2, Math.ceil((half - aEnd) / (2 * DEG)));
    for (let i = 0; i < n; i++) out.push(polar(rf, half + (aEnd - half) * i / n));
  }
  out.push(...right);
  const tipA = cw(right[right.length - 1]);
  if (!pointed && tipA > 1e-9) {                                      // tip arc to the centre line
    const n = Math.max(2, Math.ceil(tipA / (2 * DEG)));
    for (let i = 1; i <= n; i++) out.push(polar(ra, tipA * (1 - i / n)));
  }
  const half1 = simplify(out, 0.002);                                // dense sampling above → thin it out (±2 µm)
  const mirror = half1.slice(0, half1.length - 1).reverse().map(([px, py]) => [-px, py]);
  return { pts: [...half1, ...mirror], undercut, pointed };
}

// Full toothed outline (CCW), first tooth centred on +y.
export function gearOutline(b, g) {
  const t = tooth(b, g), step = 2 * Math.PI / b.z, all = [];
  for (let k = 0; k < b.z; k++) {
    const a = k * step;
    const pts = k === 0 ? t.pts : t.pts.slice(1);
    for (const p of pts) all.push(rot(p, a));
  }
  return { outline: dedupe(all), undercut: t.undercut, pointed: t.pointed };
}

// Numerical tooth thickness (arc length) at radius R, for checks and readouts.
export function toothThicknessAt(outline, R, z) {
  // first tooth spans clockwise angles [-π/z, π/z]; find where its outline crosses R
  const hits = [];
  for (let i = 0; i < outline.length; i++) {
    const a = outline[i], b = outline[(i + 1) % outline.length];
    const ra = Math.hypot(...a), rb = Math.hypot(...b);
    if ((ra - R) * (rb - R) < 0) {
      const t = (R - ra) / (rb - ra), p = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
      if (Math.abs(cw(p)) < Math.PI / z) hits.push(cw(p));
    }
  }
  if (hits.length < 2) return 0;
  return (Math.max(...hits) - Math.min(...hits)) * R;
}

// Basic rack that meshes with gears of this module (teeth point +y, pitch line y = 0).
function rackOutline(g, N, back) {
  const mn = g.mn, mt = mn / Math.cos(g.beta), at = Math.atan(Math.tan(g.alpha) / Math.cos(g.beta));
  const p = Math.PI * mt, s = Math.PI * mt / 2 - g.backlash / 2;
  const ha = mn, hf = (1 + C_STAR) * mn, tanA = Math.tan(at), sinA = Math.sin(at), cosA = Math.cos(at);
  const hw = y => s / 2 - y * tanA;                                    // half tooth width at height y
  const spaceRoot = p - 2 * hw(-hf);                                   // space width at the root line
  const rho = Math.max(0, Math.min(RHO_STAR * mn, (spaceRoot / 2) * cosA / (1 - sinA) * 0.999));
  const Fy = -hf + rho * (1 - sinA);
  const x0 = -p / 2, x1 = (N - 0.5) * p, yb = -hf - back;
  const pts = [[x0, yb], [x1, yb]];
  for (let k = N - 1; k >= 0; k--) {
    const xk = k * p;
    const F = [xk + hw(Fy), Fy], C = [F[0] + rho * cosA, F[1] + rho * sinA];
    const fil = arc(C[0], C[1], rho, 1.5 * Math.PI, Math.PI + at, 6);    // root → flank (right side)
    if (k === N - 1) pts.push([x1, -hf]);
    pts.push(...(rho > 0 ? fil : [[F[0], -hf]]));
    pts.push([xk + hw(ha), ha], [xk - hw(ha), ha]);
    pts.push(...(rho > 0 ? fil.slice().reverse().map(([px, py]) => [2 * xk - px, py]) : [[2 * xk - F[0], -hf]]));
  }
  pts.push([x0, -hf]);
  return { outline: dedupe(pts), x0, x1, yb, yRoot: -hf, yTip: ha, pitch: p };
}

// ───────────────────────────── holes ─────────────────────────────
// Bore contour (CW) and its outer radius. bore: { type, d, flat, af, keyW, keyD }
function boreContour(bore) {
  if (!bore || bore.type === 'none') return null;
  const fit = bore.fit ?? FIT;
  if (bore.type === 'hex') {
    const R = (bore.af + fit) / 2 / Math.cos(Math.PI / 6);
    return { pts: cwise(circle(R, 6).map(p => rot(p, Math.PI / 6))), R };
  }
  const R = (bore.d + fit) / 2;
  if (bore.type === 'd') {                                             // D-shaft, flat on the +y side
    const h = Math.min(R * 0.98, Math.max(-R * 0.5, bore.flat + fit - R));     // flat distance from centre (flat to far side = flat + fit)
    const a = Math.acos(h / R);                                        // flat chord half-angle about +y
    const pts = arc(0, 0, R, Math.PI / 2 + a, Math.PI / 2 + 2 * Math.PI - a, 48);
    return { pts: cwise(pts), R };
  }
  if (bore.type === 'key') {                                           // keyway on +y
    const w = Math.min(bore.keyW + fit, R * 1.6) / 2, top = R + bore.keyD;
    const a = Math.asin(Math.min(0.999, w / R));
    const pts = arc(0, 0, R, Math.PI / 2 + a, Math.PI / 2 + 2 * Math.PI - a, 48);
    pts.push([w, top], [-w, top]);
    return { pts: cwise(pts), R: Math.max(R, Math.hypot(w, top)) };
  }
  return { pts: cwise(circle(R, 64)), R };
}

// Filament-saving cut-outs in the web between rIn and rOut. cut: { style, count, size, corner }
function cutouts(cut, rIn, rOut) {
  const out = [];
  if (!cut || cut.style === 'none' || cut.count < 1) return { holes: out, note: null };
  const n = Math.round(cut.count), band = rOut - rIn;
  if (band < 3) return { holes: out, note: 'No room for cut-outs: the web between the hub and the teeth is under 3 mm.' };
  if (cut.style === 'holes') {
    const rc = (rIn + rOut) / 2, maxD = Math.min(band, 2 * rc * Math.sin(Math.PI / n) - 1.6);
    const D = Math.min(cut.size, maxD);
    if (D < 1.5) return { holes: out, note: 'Round holes don\'t fit: use fewer holes or a smaller bore/hub.' };
    for (let i = 0; i < n; i++) {
      const a = Math.PI / 2 + (i / n) * 2 * Math.PI;
      out.push(cwise(circle(D / 2, 40, rc * Math.cos(a), rc * Math.sin(a))));
    }
    return { holes: out, note: D < cut.size - 1e-6 ? `Holes reduced to ${D.toFixed(1)} mm to fit.` : null };
  }
  if (cut.style === 'slots') {
    const w = Math.min(cut.size, 2 * rIn * Math.sin(Math.PI / n) - 1.2, band - 0.5);
    if (w < 1.2) return { holes: out, note: 'Slots don\'t fit: use fewer slots or a smaller bore/hub.' };
    const r0 = rIn + w / 2, r1 = rOut - w / 2;
    for (let i = 0; i < n; i++) {
      const a = Math.PI / 2 + (i / n) * 2 * Math.PI, u = [Math.cos(a), Math.sin(a)];
      const c0 = [u[0] * r0, u[1] * r0], c1 = [u[0] * r1, u[1] * r1];
      const pts = [...arc(c1[0], c1[1], w / 2, a - Math.PI / 2, a + Math.PI / 2, 12), ...arc(c0[0], c0[1], w / 2, a + Math.PI / 2, a + 1.5 * Math.PI, 12)];
      out.push(cwise(pts));
    }
    return { holes: out, note: w < cut.size - 1e-6 ? `Slots narrowed to ${w.toFixed(1)} mm to fit.` : null };
  }
  // spokes: windows between n spokes of width `size`, rounded corners
  const w = cut.size, d = w / 2, rc = Math.max(0.6, Math.min(cut.corner ?? 2, band / 3));
  const step = 2 * Math.PI / n;
  // near the hub the spokes crowd together: start the windows far enough out that
  // neighbouring spokes (plus their corner fillets) leave a gap between them
  const tIn = Math.max(Math.sqrt(Math.max(0, (rIn + rc) ** 2 - (d + rc) ** 2)), (d + rc) / Math.tan(step / 2 * 0.8));
  const rInW = Math.hypot(tIn, d + rc) - rc;
  const tOut = Math.sqrt(Math.max(0, (rOut - rc) ** 2 - (d + rc) ** 2));
  if (tOut <= tIn + 0.5) return { holes: out, note: 'Spokes too wide for this gear: use fewer or narrower spokes.' };
  for (let i = 0; i < n; i++) {
    const aA = Math.PI / 2 + i * step, aB = aA + step;
    const uA = [Math.cos(aA), Math.sin(aA)], nA = [-uA[1], uA[0]];             // window side of spoke A
    const uB = [Math.cos(aB), Math.sin(aB)], nB = [uB[1], -uB[0]];             // window side of spoke B
    const P = (u, nn, t, off) => [nn[0] * off + u[0] * t, nn[1] * off + u[1] * t];
    // fillet centres
    const cAo = P(uA, nA, tOut, d + rc), cBo = P(uB, nB, tOut, d + rc), cAi = P(uA, nA, tIn, d + rc), cBi = P(uB, nB, tIn, d + rc);
    // angle of p, taken within ±π of ref
    const near = (p, ref) => { let a = Math.atan2(p[1], p[0]); while (a - ref > Math.PI) a -= 2 * Math.PI; while (ref - a > Math.PI) a += 2 * Math.PI; return a; };
    const oA = near(cAo, aA), oB = near(cBo, aB), iA = near(cAi, aA), iB = near(cBi, aB);
    if (oB - oA <= 0.02 || iB - iA <= 0.02) return { holes: [], note: 'Spokes too wide for this gear: use fewer or narrower spokes.' };
    // CCW window: inner fillet A, side A out, outer fillet A, outer arc, outer fillet B, side B in, inner fillet B, inner arc back
    const win = [];
    win.push(...arc(cAi[0], cAi[1], rc, iA - Math.PI, aA - Math.PI / 2, 6));       // inner circle → side A
    win.push(...arc(cAo[0], cAo[1], rc, aA - Math.PI / 2, oA, 6));                 // side A → outer circle
    win.push(...arc(0, 0, rOut, oA, oB, Math.max(2, Math.ceil((oB - oA) / (3 * DEG)))).slice(1, -1));
    win.push(...arc(cBo[0], cBo[1], rc, oB, aB + Math.PI / 2, 6));                 // outer circle → side B
    win.push(...arc(cBi[0], cBi[1], rc, aB + Math.PI / 2, iB + Math.PI, 6));       // side B → inner circle
    win.push(...arc(0, 0, rInW, iB, iA, Math.max(2, Math.ceil((iB - iA) / (3 * DEG)))).slice(1, -1));
    out.push(cwise(dedupe(win)));
  }
  return { holes: out, note: null };
}

// ───────────────────────────── solids ─────────────────────────────
// ───────────────────────────── model ─────────────────────────────
function normalize(p) {
  const type = p.type || 'spur';
  return {
    type, mn: p.module, alpha: p.pressureAngle * DEG,
    beta: type === 'spur' ? 0 : Math.max(0, Math.min(45, p.helixAngle || 0)) * DEG,
    backlash: Math.max(0, p.backlash || 0), width: p.faceWidth,
  };
}

// Twist of the outer contour at height z (radians) for helical / herringbone.
function twistAt(g, r, z, hand) {
  if (g.beta === 0) return 0;
  const zz = g.type === 'herringbone' ? Math.min(z, g.width - z) : z;
  return hand * zz * Math.tan(g.beta) / r;
}
function levels(g, maxTwist) {
  if (g.beta === 0) return [0, g.width];
  let n = Math.max(4, Math.ceil(maxTwist / (0.75 * DEG)), Math.ceil(g.width / 1.0));
  if (g.type === 'herringbone' && n % 2) n++;
  return Array.from({ length: n + 1 }, (_, i) => g.width * i / n);
}

// Build one gear part. part: { teeth, shift, bore, hub:{on,d,h}, cut, hand }
function makeGearPart(g, part, name, hand, triangulate, notes) {
  const shift = part.shift;
  const b = basics(part.teeth, shift, g);
  const { outline, undercut, pointed } = gearOutline(b, g);
  const bore = boreContour(part.bore);
  const hub = part.hub && part.hub.on ? { R: part.hub.d / 2, h: part.hub.h } : null;
  if (bore && bore.R > b.rf - 1) notes.err.push(`${name}: the bore is too big for a ${part.teeth}-tooth gear (root circle ${(2 * b.rf).toFixed(1)} mm).`);
  if (hub && hub.R > b.rf - 0.5) notes.err.push(`${name}: the hub (${part.hub.d} mm) is bigger than the root circle (${(2 * b.rf).toFixed(1)} mm).`);
  if (hub && bore && hub.R < bore.R + 1) notes.err.push(`${name}: the hub needs to be at least 2 mm wider than the bore.`);
  const rim = Math.max(2, 1.25 * g.mn);
  const rIn = Math.max(bore ? bore.R : 0, hub ? hub.R : 0) + 2;
  const cut = cutouts(part.cut, rIn, b.rf - rim);
  if (cut.note && part.cut && part.cut.style !== 'none') notes.warn.push(`${name}: ${cut.note}`);
  if (undercut) notes.warn.push(`${name}: teeth are undercut (thin at the root). Profile shift ${Math.max(0, minShift(part.teeth, g)).toFixed(2)} or more fixes it.`);
  if (pointed) notes.warn.push(`${name}: teeth come to a point. Use less profile shift.`);
  if (b.topped > 0.01) notes.warn.push(`${name}: tooth tips trimmed ${b.topped.toFixed(2)} mm so they keep a flat top.`);
  const maxTwist = Math.abs(twistAt(g, b.r, g.type === 'herringbone' ? g.width / 2 : g.width, 1));
  const zs = levels(g, maxTwist);
  const mesh = notes.err.length ? null : buildSolid({
    outerAt: z => { const a = twistAt(g, b.r, z, hand); return a ? outline.map(p => rot(p, a)) : outline; },
    zs, holes: cut.holes, bore: bore ? bore.pts : null, hub,
  }, triangulate);
  const solidArea = Math.abs(area(outline)) - (bore ? Math.abs(area(bore.pts)) : 0);
  const cutArea = cut.holes.reduce((s, h) => s + Math.abs(area(h)), 0);
  return { name, kind: 'gear', b, outline, holes: [...cut.holes, ...(bore ? [bore.pts] : [])], mesh, hand,
           cutSaving: solidArea > 0 ? cutArea / solidArea : 0, hub, height: g.width + (hub ? hub.h : 0) };
}

function makeRackPart(g, rack, name, triangulate, notes) {
  const back = Math.max(1, rack.back);
  const R = rackOutline(g, Math.max(1, Math.round(rack.teeth)), back);
  const holes = [];
  if (rack.holes && rack.holes.count > 0) {
    const D = rack.holes.d + FIT, yc = (R.yb + R.yRoot) / 2;
    if (D > back - 1.5) notes.warn.push(`${name}: mounting holes need a back at least ${(D + 1.5).toFixed(1)} mm tall.`);
    else {
      const n = Math.round(rack.holes.count), span = R.x1 - R.x0, edge = Math.max(D, span / (2 * n));
      for (let i = 0; i < n; i++) {
        const x = n === 1 ? (R.x0 + R.x1) / 2 : R.x0 + edge + (span - 2 * edge) * i / (n - 1);
        holes.push(cwise(circle(D / 2, 40, x, yc)));
      }
    }
  }
  const shear = z => (g.beta === 0 ? 0 : (g.type === 'herringbone' ? Math.min(z, g.width - z) : z) * Math.tan(g.beta));
  const zs = levels(g, 0);
  const mesh = buildSolid({ outerAt: z => { const s = shear(z); return s ? R.outline.map(([x, y]) => [x + s, y]) : R.outline; }, zs, holes, bore: null, hub: null }, triangulate);
  return { name, kind: 'rack', rack: R, outline: R.outline, holes, mesh, height: g.width, length: R.x1 - R.x0 };
}

// Closed solid: outer contour (CCW) sampled at heights zs (twisted/sheared per height),
// cut-outs (CW) straight through, bore (CW) through body and hub, optional hub boss on top.
// triangulate(contour, holes) → index triples into [contour, ...holes].
function buildSolid({ outerAt, zs, holes, bore, hub }, triangulate) {
  const m = new Mesh(), pos = m.pos, tri = m.tri;
  const ring = (pts, z) => { const base = pos.length / 3; for (const [x, y] of pts) pos.push(x, y, z); return pts.map((_, i) => base + i); };
  const wall = (i0, i1) => { const n = i0.length; for (let i = 0; i < n; i++) { const j = (i + 1) % n; tri.push(i0[i], i0[j], i1[j], i0[i], i1[j], i1[i]); } };
  const T = zs[zs.length - 1], topZ = hub ? T + hub.h : T;
  const outer = zs.map(z => { const pts = outerAt(z); return { pts, idx: ring(pts, z) }; });
  for (let k = 0; k < zs.length - 1; k++) wall(outer[k].idx, outer[k + 1].idx);
  const hs = holes.map(h => { const a = ring(h, 0), b = ring(h, T); wall(a, b); return { pts: h, bot: a, top: b }; });
  let bo = null;
  if (bore) { const a = ring(bore, 0), b = ring(bore, topZ); wall(a, b); bo = { pts: bore, bot: a, top: b }; }
  let hb = null;
  if (hub) { const c = circle(hub.R, 96), a = ring(c, T), b = ring(c, topZ); wall(a, b); hb = { pts: c, bot: a, top: b }; }
  const cap = (contour, holeList, up) => {   // each: { pts, idx }
    const loops = [contour, ...holeList];
    const all = loops.flatMap(l => l.pts), ids = loops.flatMap(l => l.idx);
    const faces = triangulate(contour.pts, holeList.map(h => h.pts));
    for (const f of faces) {
      let [a, b, c] = f;
      const s = (all[b][0] - all[a][0]) * (all[c][1] - all[a][1]) - (all[b][1] - all[a][1]) * (all[c][0] - all[a][0]);
      if (Math.abs(s) < 1e-14) continue;
      if ((s > 0) !== up) [b, c] = [c, b];
      tri.push(ids[a], ids[b], ids[c]);
    }
  };
  const top = outer[outer.length - 1], bot = outer[0];
  cap({ pts: bot.pts, idx: bot.idx }, [...hs.map(h => ({ pts: h.pts, idx: h.bot })), ...(bo ? [{ pts: bo.pts, idx: bo.bot }] : [])], false);
  const topHoles = hs.map(h => ({ pts: h.pts, idx: h.top }));
  if (hb) topHoles.push({ pts: hb.pts.slice().reverse(), idx: hb.bot.slice().reverse() });
  else if (bo) topHoles.push({ pts: bo.pts, idx: bo.top });
  cap({ pts: top.pts, idx: top.idx }, topHoles, true);
  if (hb) cap({ pts: hb.pts, idx: hb.top }, bo ? [{ pts: bo.pts, idx: bo.top }] : [], true);
  return m;
}

// Build everything the page needs.
// p: { type, module, pressureAngle, helixAngle, backlash, faceWidth,
//      g1: { teeth, shift, autoShift, bore, hub, cut },
//      second: 'none' | 'gear' | 'rack', g2: {...}, rack: { teeth, back, holes: { count, d } } }
// Profile shift a gear part ends up with (auto = just enough to avoid undercut).
const shiftOf = (part, g) => (part.autoShift ? Math.max(0, +minShift(part.teeth, g).toFixed(3)) : (part.shift || 0));

// Outside (tip) diameter of gear 1 for the given parameters.
export function outsideDiameter(p) {
  const g = normalize(p);
  return 2 * basics(p.g1.teeth, shiftOf(p.g1, g), g).ra;
}

// Module that gives gear 1 the requested outside diameter. Everything scales with
// the module except backlash and tip trimming, so a few proportional steps converge.
export function moduleForOutside(od, p) {
  const g0 = normalize({ ...p, module: 1 });
  let m = od / (p.g1.teeth / Math.cos(g0.beta) + 2 + 2 * shiftOf(p.g1, g0));
  for (let i = 0; i < 12; i++) {
    const got = outsideDiameter({ ...p, module: m });
    if (!(got > 0)) break;
    m *= od / got;
    if (Math.abs(got - od) < 1e-6) break;
  }
  return m;
}

export function makeModel(p, triangulate) {
  const g = normalize(p), notes = { err: [], warn: [] };
  const g1 = makeGearPart(g, { ...p.g1, shift: shiftOf(p.g1, g) }, 'Gear 1', 1, triangulate, notes);
  const parts = [g1];
  const info = { g };
  // placement for the preview: rotation about z (rad) and position; motion as a function of gear-1 angle
  g1.place = { x: 0, y: 0, rot: p.second === 'gear' ? -Math.PI / 2 : 0 };
  g1.ratio = 1;
  if (p.second === 'gear') {
    const g2 = makeGearPart(g, { ...p.g2, shift: shiftOf(p.g2, g) }, 'Gear 2', -1, triangulate, notes);
    const z1 = g1.b.z, z2 = g2.b.z, at = g1.b.at;
    const invAw = inv(at) + 2 * Math.tan(g.alpha) * (g1.b.x + g2.b.x) / (z1 + z2);
    const aw = invInverse(invAw);
    const a = (g1.b.r + g2.b.r) * Math.cos(at) / Math.cos(aw);
    g2.place = { x: a, y: 0, rot: Math.PI / 2 - Math.PI / z2 };
    g2.ratio = -z1 / z2;
    parts.push(g2);
    const pb = Math.PI * g1.b.mt * Math.cos(at);
    const eA = (Math.sqrt(g1.b.ra ** 2 - g1.b.rb ** 2) + Math.sqrt(g2.b.ra ** 2 - g2.b.rb ** 2) - a * Math.sin(aw)) / pb;
    Object.assign(info, { centre: a, workingAngle: aw / DEG, contact: eA, ratio: z2 / z1 });
    if (eA < 1.1) notes.warn.push(`Contact ratio is only ${eA.toFixed(2)}: the gears may run rough. Add teeth or reduce profile shift.`);
  } else if (p.second === 'rack') {
    const rk = makeRackPart(g, p.rack, 'Rack', triangulate, notes);
    const n = rk.rack, pitch = n.pitch;
    // rack above the gear, teeth down (rotated 180°), a space centred over gear 1's top tooth
    const k = Math.floor((Math.round(p.rack.teeth) - 1) / 2);
    rk.place = { x: pitch / 2 + k * pitch, y: g1.b.r + g1.b.x * g.mn, rot: Math.PI };
    rk.linear = -g1.b.r;                                                  // rack x per radian of gear 1
    parts.push(rk);
    const pb = Math.PI * g1.b.mt * Math.cos(g1.b.at);
    const eA = (Math.sqrt(g1.b.ra ** 2 - g1.b.rb ** 2) - g1.b.r * Math.sin(g1.b.at) + (g.mn - g1.b.x * g.mn) / Math.sin(g1.b.at)) / pb;
    Object.assign(info, { contact: eA, travel: Math.PI * 2 * g1.b.r });
  }
  if (g.beta > 0) info.overlap = g.width * Math.sin(g.beta) / (Math.PI * g.mn);
  for (const part of parts) if (part.mesh) part.volume = meshVolume(part.mesh);
  return { g, parts, info, errors: notes.err, warnings: notes.warn };
}

export function meshVolume(m) {
  const P = m.pos, T = m.tri; let v = 0;
  for (let i = 0; i < T.length; i += 3) {
    const a = T[i] * 3, b = T[i + 1] * 3, c = T[i + 2] * 3;
    v += P[a] * (P[b + 1] * P[c + 2] - P[b + 2] * P[c + 1]) - P[a + 1] * (P[b] * P[c + 2] - P[b + 2] * P[c]) + P[a + 2] * (P[b] * P[c + 1] - P[b + 1] * P[c]);
  }
  return v / 6;
}

// ───────────────────────────── 2D exports ─────────────────────────────
// Parts laid out side by side (not meshed) for laser cutting / CAD, bottom face.
function layout2D(model, gap = 6) {
  const out = [];
  let x = 0;
  for (const part of model.parts) {
    const loops = [part.outline, ...part.holes];
    let mnx = Infinity, mny = Infinity, mxx = -Infinity, mxy = -Infinity;
    for (const [px, py] of part.outline) { mnx = Math.min(mnx, px); mxx = Math.max(mxx, px); mny = Math.min(mny, py); mxy = Math.max(mxy, py); }
    out.push({ name: part.name, loops: loops.map(l => l.map(([px, py]) => [px - mnx + x, py - mny])) });
    x += mxx - mnx + gap;
  }
  return out;
}
export function toSVG(model) {
  const lay = layout2D(model);
  let mx = 0, my = 0;
  for (const p of lay) for (const l of p.loops) for (const [x, y] of l) { mx = Math.max(mx, x); my = Math.max(my, y); }
  const f = v => (Math.round(v * 1000) / 1000).toString();
  const paths = lay.map(p => `  <path id="${p.name.replace(/\s+/g, '-').toLowerCase()}" fill-rule="evenodd" fill="none" stroke="#000" stroke-width="0.1" d="` +
    p.loops.map(l => 'M' + l.map(([x, y]) => `${f(x)},${f(my - y)}`).join('L') + 'Z').join(' ') + '"/>').join('\n');
  return `<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="${f(mx)}mm" height="${f(my)}mm" viewBox="0 0 ${f(mx)} ${f(my)}">\n${paths}\n</svg>\n`;
}
export function toDXF(model) {
  const lay = layout2D(model), L = [];
  const w = (...a) => L.push(...a);
  w('0', 'SECTION', '2', 'HEADER', '9', '$ACADVER', '1', 'AC1009', '9', '$INSUNITS', '70', '4', '0', 'ENDSEC');
  w('0', 'SECTION', '2', 'ENTITIES');
  for (const p of lay) for (const l of p.loops) {
    w('0', 'POLYLINE', '8', p.name.replace(/\s+/g, '_').toUpperCase(), '66', '1', '70', '1', '10', '0', '20', '0', '30', '0');
    for (const [x, y] of l) w('0', 'VERTEX', '8', p.name.replace(/\s+/g, '_').toUpperCase(), '10', x.toFixed(4), '20', y.toFixed(4), '30', '0');
    w('0', 'SEQEND');
  }
  w('0', 'ENDSEC', '0', 'EOF');
  return L.join('\n') + '\n';
}

// ───────────────────────────── 3MF plan ─────────────────────────────
// Pack the parts onto plates of the chosen printer (single filament).
export function makePrintPlan(model, printerKey) {
  const printer = PRINTERS[printerKey] || PRINTERS.p1s, bed = printer.bed, M = 8, GAP = 6;
  const ex = printer.exclude;
  const plates = [], errors = [];
  let cur = null, x = M, y = M, rowH = 0;
  const newPlate = () => { cur = { name: `Plate ${plates.length + 1}`, items: [], tower: null }; plates.push(cur); x = M; y = M; rowH = 0; };
  newPlate();
  for (const part of model.parts) {
    if (!part.mesh) continue;
    const b = part.mesh.bounds(), w = b.max[0] - b.min[0], d = b.max[1] - b.min[1];
    if (w > bed[0] - 2 * M || d > bed[1] - 2 * M) { errors.push(`${part.name} (${w.toFixed(0)} × ${d.toFixed(0)} mm) doesn't fit the ${printer.label} bed.`); continue; }
    if (part.height > printer.maxZ) { errors.push(`${part.name} is taller than the ${printer.label} can print.`); continue; }
    for (let tries = 0; tries < 4; tries++) {
      if (ex && x < ex[2] + 3 && y < ex[3] + 3) x = ex[2] + 3;
      if (x + w > bed[0] - M) { x = M; y += rowH + GAP; rowH = 0; continue; }
      if (y + d > bed[1] - M) { newPlate(); continue; }
      break;
    }
    const obj = { name: part.name, extruder: 1, kind: part.kind, volumes: [{ name: part.name, mesh: part.mesh, extruder: 1 }] };
    cur.items.push({ obj, x: x + w / 2, y: y + d / 2 });
    x += w + GAP; rowH = Math.max(rowH, d);
  }
  const objects = plates.flatMap(p => p.items.map(i => i.obj));
  return { printer, plates: plates.filter(p => p.items.length), objects, errors };
}
