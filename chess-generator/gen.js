// Chess Set Generator — core logic (no DOM).
//
// Builds a two-colour chessboard (dark squares inlaid into the top layers),
// optionally split into four dovetailed quarters, sizes a set of pieces to the
// board, lays everything out on Bambu Lab build plates and writes a Bambu
// Studio project .3mf (plates, per-part filaments and printer presets).
//
// All lengths are millimetres.

import { PRINTERS, Mesh, triangulate, parseSTL, plateColumns, plateOrigin, build3MFEntries, makeZip, build3MF } from '../lib/print3d.js';
export { PRINTERS, Mesh, triangulate, parseSTL, plateColumns, plateOrigin, build3MFEntries, makeZip, build3MF };

export const IN = 25.4;

export const PIECE_ORDER = ['king', 'queen', 'bishop', 'knight', 'rook', 'pawn'];
export const PER_ARMY = { king: 1, queen: 1, bishop: 2, knight: 2, rook: 2, pawn: 8 };

const PLATE_MARGIN = 8;      // keep parts this far from the bed edge
const PIECE_GAP = 6;         // gap between pieces on a plate
const TOWER = [35, 45];      // space kept free for the prime tower (w × d)

const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const isDark = (file, rank) => (file + rank) % 2 === 0;   // a1 is dark
export const squareName = (f, r) => 'abcdefgh'[f] + (r + 1);

// Offset an open polyline to the left of its direction of travel by d
// (mitred corners). Endpoints move perpendicular to their segment.
function offsetLeft(pts, d) {
  const n = pts.length, out = [];
  const seg = i => {
    const [x0, y0] = pts[i], [x1, y1] = pts[i + 1];
    const L = Math.hypot(x1 - x0, y1 - y0);
    const ux = (x1 - x0) / L, uy = (y1 - y0) / L;
    return { ux, uy, nx: -uy, ny: ux };            // left normal
  };
  for (let i = 0; i < n; i++) {
    if (i === 0 || i === n - 1) {
      const s = seg(i === 0 ? 0 : n - 2);
      out.push([pts[i][0] + s.nx * d, pts[i][1] + s.ny * d]);
    } else {
      const a = seg(i - 1), b = seg(i);
      const mx = a.nx + b.nx, my = a.ny + b.ny;
      const k = d / (1 + a.nx * b.nx + a.ny * b.ny); // |m|² / 2 = 1 + cos
      out.push([pts[i][0] + mx * k, pts[i][1] + my * k]);
    }
  }
  return out;
}

// ───────────────────────────── board ─────────────────────────────
// p: { size, thickness, inlay, split, clearance }
export function boardGeometry(p) {
  const W = p.size, s = W / 8, T = p.thickness;
  const d = clamp(p.inlay, 0.2, T - 0.6);           // inlay depth (dark squares)
  const n = p.split ? 2 : 1, per = 8 / n;
  const c = p.split ? clamp(p.clearance, 0, 0.5) : 0;
  // dovetail: neck at the seam, wider head inside the neighbour
  const neck = clamp(0.28 * s, 5, 14);
  const head = neck + 0.6 * clamp(0.22 * s, 4, 12);
  const D = Math.min(clamp(0.22 * s, 4, 12), s / 2 - head / 2 - 1.5);
  const X0 = W / 2, Y0 = W / 2;
  // bump direction for seam segment k (+1 = towards +x / +y)
  const sign = k => (k % 2 === 0 ? 1 : -1);

  // Ideal seam polyline for one square edge, in the given travel direction.
  function idealSegment(vertical, k, forward) {
    const tc = (k + 0.5) * s, sg = sign(k);
    let pts;
    if (vertical) {
      pts = [[X0, k * s], [X0, tc - neck / 2], [X0 + sg * D, tc - head / 2],
             [X0 + sg * D, tc + head / 2], [X0, tc + neck / 2], [X0, (k + 1) * s]];
    } else {
      pts = [[k * s, Y0], [tc - neck / 2, Y0], [tc - head / 2, Y0 + sg * D],
             [tc + head / 2, Y0 + sg * D], [tc + neck / 2, Y0], [(k + 1) * s, Y0]];
    }
    return forward ? pts : pts.reverse();
  }

  const quarters = [];
  for (let qj = 0; qj < n; qj++) for (let qi = 0; qi < n; qi++) {
    const f0 = qi * per, r0 = qj * per, f1 = f0 + per, r1 = r0 + per;
    const seam = { bottom: qj > 0, right: qi < n - 1, top: qj < n - 1, left: qi > 0 };
    const xa = f0 * s + (seam.left ? c : 0), xb = f1 * s - (seam.right ? c : 0);
    const ya = r0 * s + (seam.bottom ? c : 0), yb = r1 * s - (seam.top ? c : 0);

    // For each side: list of { k, male, pts:[4 offset bump points] } in travel order.
    const sides = { bottom: [], right: [], top: [], left: [] };
    const addSide = (name, vertical, forward, ks) => {
      if (!seam[name]) return;
      for (const k of ks) {
        const ideal = idealSegment(vertical, k, forward);
        const off = offsetLeft(ideal, c).slice(1, 5);
        // travel direction and right-hand normal
        const [ax, ay] = ideal[0], [bx, by] = ideal[5];
        const L = Math.hypot(bx - ax, by - ay), ux = (bx - ax) / L, uy = (by - ay) / L;
        const rx = uy, ry = -ux;
        const bump = vertical ? [sign(k), 0] : [0, sign(k)];
        const male = bump[0] * rx + bump[1] * ry > 0;
        sides[name].push({ k, male, pts: off });
      }
    };
    const range = (a, b) => { const r = []; for (let i = a; i < b; i++) r.push(i); return r; };
    addSide('bottom', false, true, range(f0, f1));
    addSide('right', true, true, range(r0, r1));
    addSide('top', false, false, range(f0, f1).reverse());
    addSide('left', true, false, range(r0, r1).reverse());

    const outline = [[xa, ya]];
    for (const b of sides.bottom) outline.push(...b.pts);
    outline.push([xb, ya]);
    for (const b of sides.right) outline.push(...b.pts);
    outline.push([xb, yb]);
    for (const b of sides.top) outline.push(...b.pts);
    outline.push([xa, yb]);
    for (const b of sides.left) outline.push(...b.pts);

    const findBump = (side, k) => sides[side].find(b => b.k === k);
    const cells = [];
    for (let r = r0; r < r1; r++) for (let f = f0; f < f1; f++) {
      const x0 = Math.max(f * s, xa), x1 = Math.min((f + 1) * s, xb);
      const y0 = Math.max(r * s, ya), y1 = Math.min((r + 1) * s, yb);
      const poly = [[x0, y0]];
      const notch = (side, k) => { const b = findBump(side, k); if (b && !b.male) poly.push(...b.pts); };
      if (r === r0) notch('bottom', f);
      poly.push([x1, y0]);
      if (f === f1 - 1) notch('right', r);
      poly.push([x1, y1]);
      if (r === r1 - 1) notch('top', f);
      poly.push([x0, y1]);
      if (f === f0) notch('left', r);
      cells.push({ poly, dark: isDark(f, r), square: squareName(f, r) });
    }
    // tabs: coloured like the neighbour's square they sit in
    const tabs = [];
    const neighbour = { bottom: k => [k, r0 - 1], right: k => [f1, k], top: k => [k, r1], left: k => [f0 - 1, k] };
    for (const side of Object.keys(sides)) for (const b of sides[side]) {
      if (!b.male) continue;
      const [nf, nr] = neighbour[side](b.k);
      tabs.push({ poly: b.pts.slice(), dark: isDark(nf, nr), square: squareName(nf, nr) });
    }

    const light = new Mesh(), dark = new Mesh();
    light.addPrism(outline, 0, T - d);
    for (const cell of [...cells, ...tabs]) (cell.dark ? dark : light).addPrism(cell.poly, T - d, T);

    const name = 'Board ' + squareName(f0, r0) + '-' + squareName(f1 - 1, r1 - 1);
    quarters.push({ name, qi, qj, f0, r0, outline, cells, tabs, light, dark, bounds: light.bounds() });
  }
  return { W, s, T, d, c, n, dovetail: { neck, head, depth: D }, quarters };
}

// ───────────────────────────── pieces ─────────────────────────────
// Uniformly scale a source piece so the king base is `kingPct` of a square.
export function pieceScale(kingMesh, s, kingPct) {
  const b = kingMesh.bounds();
  const base = Math.max(b.max[0] - b.min[0], b.max[1] - b.min[1]);
  return (kingPct * s) / base;
}

// Scaled copy of a piece mesh, centred on its XY bounds, standing on z = 0.
export function scalePiece(mesh, k) {
  const b = mesh.bounds();
  const cx = (b.min[0] + b.max[0]) / 2, cy = (b.min[1] + b.max[1]) / 2, z0 = b.min[2];
  const out = new Mesh();
  out.pos = new Float32Array(mesh.pos.length);
  for (let i = 0; i < mesh.pos.length; i += 3) {
    out.pos[i] = (mesh.pos[i] - cx) * k;
    out.pos[i + 1] = (mesh.pos[i + 1] - cy) * k;
    out.pos[i + 2] = (mesh.pos[i + 2] - z0) * k;
  }
  out.tri = mesh.tri;
  return out;
}

// ───────────────────────────── plates ─────────────────────────────
// Shelf-pack rectangles (w × d) onto plates. Returns [[{item,x,y}...], ...]
// where x, y is the item's centre in plate coordinates.
function packPieces(items, bed, exclude) {
  const plates = [];
  let cur = [], x = PLATE_MARGIN, y = PLATE_MARGIN, rowH = 0;
  const usableW = bed[0] - PLATE_MARGIN, usableD = bed[1] - PLATE_MARGIN;
  const hitsExclude = (x0, y0, w, h) => exclude &&
    x0 < exclude[2] + 3 && x0 + w > exclude[0] - 3 && y0 < exclude[3] + 3 && y0 + h > exclude[1] - 3;
  for (const it of items) {
    let placed = false;
    for (let attempt = 0; attempt < 3 && !placed; attempt++) {
      if (hitsExclude(x, y, it.w, it.d)) x = exclude[2] + 3;
      if (x + it.w > usableW) { x = PLATE_MARGIN; y += rowH + PIECE_GAP; rowH = 0; continue; }
      if (y + it.d > usableD) {
        if (!cur.length) return null;                // a single piece is bigger than the bed
        plates.push(cur); cur = []; x = PLATE_MARGIN; y = PLATE_MARGIN; rowH = 0; continue;
      }
      cur.push({ item: it, x: x + it.w / 2, y: y + it.d / 2 });
      x += it.w + PIECE_GAP; rowH = Math.max(rowH, it.d); placed = true;
    }
    if (!placed) return null;
  }
  if (cur.length) plates.push(cur);
  return plates;
}

// Build the full print plan.
// opts: { size, thickness, inlay, split, clearance, kingPct, printer, includePieces, pieces: {king: Mesh, ...} }
export function makePlan(opts) {
  const printer = PRINTERS[opts.printer] || PRINTERS.p1s;
  const bed = printer.bed;
  const board = boardGeometry(opts);
  const warnings = [], errors = [];
  const objects = [];                 // { name, volumes:[{name, mesh, extruder}], extruder }
  const plates = [];                  // { name, items:[{obj, x, y}], tower:[x,y]|null }

  // Board pieces, one per plate.
  let towerBlocked = false;
  for (const q of board.quarters) {
    const b = q.bounds, w = b.max[0] - b.min[0], h = b.max[1] - b.min[1];
    if (w > bed[0] - 2 * PLATE_MARGIN + 0.01 || h > bed[1] - 2 * PLATE_MARGIN + 0.01) {
      errors.push(`${q.name} is ${w.toFixed(1)} × ${h.toFixed(1)} mm — bigger than the ${printer.label} bed (${bed[0]} × ${bed[1]} mm)` +
        (opts.split ? '. Use a smaller board.' : '. Tick "Split into 4".'));
    }
    const obj = { name: q.name, extruder: 1, kind: 'board', volumes: [
      { name: 'light squares', mesh: q.light, extruder: 1 },
      { name: 'dark squares', mesh: q.dark, extruder: 2 },
    ] };
    objects.push(obj);
    // quarter at the back-left, prime tower at the front-right
    const cx = PLATE_MARGIN + w / 2, cy = bed[1] - PLATE_MARGIN - h / 2;
    const tower = [bed[0] - PLATE_MARGIN - TOWER[0], PLATE_MARGIN + 2];
    const towerFree = PLATE_MARGIN + w + 4 <= tower[0] || bed[1] - PLATE_MARGIN - h - 4 >= tower[1] + TOWER[1];
    if (!towerFree) towerBlocked = true;
    plates.push({ name: q.name, items: [{ obj, x: cx, y: cy }], tower: towerFree ? tower : null, quarter: q });
  }
  if (towerBlocked && !errors.length) warnings.push(`No room left on the ${printer.label} plate for a prime tower next to the board${board.n > 1 ? ' quarter' : ''}. ` +
    'Bambu Studio will flag it: drag the tower into a gap or switch it off (Others → Prime tower). The board only changes colour for its top few layers, so switching it off is fine.');

  let scale = null, pieceInfo = [];
  if (opts.includePieces && opts.pieces && opts.pieces.king) {
    scale = pieceScale(opts.pieces.king, board.s, opts.kingPct);
    for (const army of ['light', 'dark']) {
      const extruder = army === 'light' ? 1 : 2;
      const items = [];
      for (const type of PIECE_ORDER) {
        const src = opts.pieces[type];
        if (!src) continue;
        const mesh = scalePiece(src, scale);
        const b = mesh.bounds();
        const w = b.max[0] - b.min[0], dpt = b.max[1] - b.min[1], h = b.max[2];
        const obj = { name: (army === 'light' ? 'Light ' : 'Dark ') + type, extruder, kind: 'piece', type, army,
                      volumes: [{ name: type, mesh, extruder, shared: type }] };
        objects.push(obj);
        // base = the narrower footprint side, so a knight's overhanging head doesn't count
        if (army === 'light') pieceInfo.push({ type, height: h, base: Math.min(w, dpt), w, d: dpt });
        if (h > printer.maxZ) errors.push(`The ${type} would be ${h.toFixed(0)} mm tall — taller than the ${printer.label} can print (${printer.maxZ} mm).`);
        for (let i = 0; i < PER_ARMY[type]; i++) items.push({ obj, w, d: dpt });
      }
      // big pieces first, then by depth so rows stay tidy
      items.sort((a, b) => b.d - a.d || b.w - a.w);
      const packed = packPieces(items, bed, printer.exclude);
      if (!packed) { errors.push('A piece is bigger than the build plate.'); continue; }
      packed.forEach((list, i) => plates.push({
        name: (army === 'light' ? 'Light army' : 'Dark army') + (packed.length > 1 ? ` ${i + 1}/${packed.length}` : ''),
        items: list.map(p => ({ obj: p.item.obj, x: p.x, y: p.y })), tower: null,
      }));
    }
  }
  return { printer, board, objects, plates, scale, pieceInfo, warnings, errors };
}
