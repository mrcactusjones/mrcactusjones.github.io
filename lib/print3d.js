// Shared 3D-printing helpers for the Toolkit's generators (no DOM, runs in Node too):
// meshes, STL in/out, Bambu Lab printer presets, plate layout maths, and a
// Bambu Studio project .3mf writer with its own zip packer.
// All lengths are millimetres.

// Printer presets. Names must match Bambu Studio's system presets exactly.
export const PRINTERS = {
  p1s: { label: 'Bambu Lab P1S',       bed: [256, 256], maxZ: 250, exclude: [0, 0, 18, 28],
         printer: 'Bambu Lab P1S 0.4 nozzle',       model: 'Bambu Lab P1S',
         process: '0.20mm Standard @BBL X1C', filament: 'Bambu PLA Basic @BBL X1C' },
  x1c: { label: 'Bambu Lab X1 Carbon', bed: [256, 256], maxZ: 250, exclude: [0, 0, 18, 28],
         printer: 'Bambu Lab X1 Carbon 0.4 nozzle', model: 'Bambu Lab X1 Carbon',
         process: '0.20mm Standard @BBL X1C', filament: 'Bambu PLA Basic @BBL X1C' },
  p1p: { label: 'Bambu Lab P1P',       bed: [256, 256], maxZ: 250, exclude: [0, 0, 18, 28],
         printer: 'Bambu Lab P1P 0.4 nozzle',       model: 'Bambu Lab P1P',
         process: '0.20mm Standard @BBL P1P', filament: 'Bambu PLA Basic @BBL P1P' },
  a1:  { label: 'Bambu Lab A1',        bed: [256, 256], maxZ: 256, exclude: null,
         printer: 'Bambu Lab A1 0.4 nozzle',        model: 'Bambu Lab A1',
         process: '0.20mm Standard @BBL A1',  filament: 'Bambu PLA Basic @BBL A1' },
  a1m: { label: 'Bambu Lab A1 mini',   bed: [180, 180], maxZ: 180, exclude: null,
         printer: 'Bambu Lab A1 mini 0.4 nozzle',   model: 'Bambu Lab A1 mini',
         process: '0.20mm Standard @BBL A1M', filament: 'Bambu PLA Basic @BBL A1M' },
};

// ───────────────────────────── 2D helpers ─────────────────────────────
export function signedArea(p) {
  let a = 0;
  for (let i = 0, n = p.length; i < n; i++) {
    const [x0, y0] = p[i], [x1, y1] = p[(i + 1) % n];
    a += x0 * y1 - x1 * y0;
  }
  return a / 2;
}
const cross3 = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);

// Ear-clipping triangulation of a simple CCW polygon. Every input vertex is
// used, so caps always match the side walls (no T-junctions).
export function triangulate(pts) {
  const idx = pts.map((_, i) => i), tris = [];
  const inTri = (p, a, b, c, strict) => {
    const e = strict ? 1e-9 : -1e-9;
    return cross3(a, b, p) > e && cross3(b, c, p) > e && cross3(c, a, p) > e;
  };
  const tryClip = strict => {
    for (let i = 0; i < idx.length; i++) {
      const ia = idx[(i + idx.length - 1) % idx.length], ib = idx[i], ic = idx[(i + 1) % idx.length];
      const a = pts[ia], b = pts[ib], c = pts[ic];
      if (cross3(a, b, c) <= 1e-9) continue;               // reflex / flat
      let blocked = false;
      for (const j of idx) {
        if (j === ia || j === ib || j === ic) continue;
        if (inTri(pts[j], a, b, c, strict)) { blocked = true; break; }
      }
      if (blocked) continue;
      tris.push(ia, ib, ic);
      idx.splice(i, 1);
      return true;
    }
    return false;
  };
  let guard = pts.length * 4;
  while (idx.length > 3 && guard-- > 0) {
    if (!tryClip(false) && !tryClip(true)) throw new Error('triangulation failed');
  }
  tris.push(idx[0], idx[1], idx[2]);
  return tris;
}

// ───────────────────────────── meshes ─────────────────────────────
// Indexed triangle mesh: pos = [x,y,z,...], tri = [a,b,c,...] (CCW = outward).
export class Mesh {
  constructor() { this.pos = []; this.tri = []; }
  get vertexCount() { return this.pos.length / 3; }
  get triCount() { return this.tri.length / 3; }
  // Closed prism shell over a CCW polygon from z0 to z1.
  addPrism(poly, z0, z1) {
    if (signedArea(poly) < 0) poly = poly.slice().reverse();
    const n = poly.length, base = this.vertexCount;
    for (const [x, y] of poly) this.pos.push(x, y, z0);
    for (const [x, y] of poly) this.pos.push(x, y, z1);
    const cap = triangulate(poly);
    for (let i = 0; i < cap.length; i += 3) {
      this.tri.push(base + n + cap[i], base + n + cap[i + 1], base + n + cap[i + 2]);   // top (+z)
      this.tri.push(base + cap[i], base + cap[i + 2], base + cap[i + 1]);               // bottom (−z)
    }
    for (let i = 0; i < n; i++) {
      const j = (i + 1) % n;
      this.tri.push(base + i, base + j, base + n + j, base + i, base + n + j, base + n + i);
    }
    return this;
  }
  bounds() {
    const mn = [Infinity, Infinity, Infinity], mx = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < this.pos.length; i += 3)
      for (let k = 0; k < 3; k++) { const v = this.pos[i + k]; if (v < mn[k]) mn[k] = v; if (v > mx[k]) mx[k] = v; }
    return { min: mn, max: mx };
  }
}

// Parse a binary or ASCII STL into an indexed mesh (vertices welded).
export function parseSTL(buf) {
  const u8 = new Uint8Array(buf), dv = new DataView(buf);
  let tris = [];
  const isBinary = u8.length >= 84 && 84 + dv.getUint32(80, true) * 50 === u8.length;
  let raw;
  if (isBinary) {
    const n = dv.getUint32(80, true);
    raw = new Float32Array(n * 9);
    for (let i = 0; i < n; i++) {
      const o = 84 + i * 50 + 12;
      for (let k = 0; k < 9; k++) raw[i * 9 + k] = dv.getFloat32(o + k * 4, true);
    }
  } else {
    const text = new TextDecoder().decode(u8);
    const re = /vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)/g;
    const vals = [];
    let m;
    while ((m = re.exec(text))) vals.push(+m[1], +m[2], +m[3]);
    raw = new Float32Array(vals);
  }
  const map = new Map(), pos = [], tri = new Uint32Array(raw.length / 3);
  for (let i = 0; i < raw.length; i += 3) {
    const key = raw[i] + ',' + raw[i + 1] + ',' + raw[i + 2];
    let id = map.get(key);
    if (id === undefined) { id = pos.length / 3; map.set(key, id); pos.push(raw[i], raw[i + 1], raw[i + 2]); }
    tri[i / 3] = id;
  }
  // drop degenerate triangles
  const keep = [];
  for (let i = 0; i < tri.length; i += 3) {
    const a = tri[i], b = tri[i + 1], c = tri[i + 2];
    if (a !== b && b !== c && a !== c) keep.push(a, b, c);
  }
  const m = new Mesh();
  m.pos = Float32Array.from(pos);
  m.tri = Uint32Array.from(keep);
  return m;
}

export function plateColumns(count) {             // Bambu Studio's compute_colum_count()
  const v = Math.sqrt(count), r = Math.round(v);
  return v > r ? r + 1 : r;
}
export function plateOrigin(index, count, bed) {  // plate index is 0-based
  const cols = plateColumns(count), row = Math.floor(index / cols), col = index % cols;
  return [col * bed[0] * 1.2, -row * bed[1] * 1.2];
}

// Binary STL of an indexed mesh (normals computed per triangle).
export function stlBinary(mesh, header = 'Rob\'s Toolkit') {
  const P = mesh.pos, T = mesh.tri, n = T.length / 3;
  const buf = new ArrayBuffer(84 + n * 50), dv = new DataView(buf);
  const h = new TextEncoder().encode(header.slice(0, 79));
  new Uint8Array(buf, 0, 80).set(h);
  dv.setUint32(80, n, true);
  let o = 84;
  for (let i = 0; i < T.length; i += 3) {
    const a = T[i] * 3, b = T[i + 1] * 3, c = T[i + 2] * 3;
    const ux = P[b] - P[a], uy = P[b + 1] - P[a + 1], uz = P[b + 2] - P[a + 2];
    const vx = P[c] - P[a], vy = P[c + 1] - P[a + 1], vz = P[c + 2] - P[a + 2];
    let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    const L = Math.hypot(nx, ny, nz) || 1;
    dv.setFloat32(o, nx / L, true); dv.setFloat32(o + 4, ny / L, true); dv.setFloat32(o + 8, nz / L, true);
    o += 12;
    for (const k of [a, b, c]) { dv.setFloat32(o, P[k], true); dv.setFloat32(o + 4, P[k + 1], true); dv.setFloat32(o + 8, P[k + 2], true); o += 12; }
    o += 2;
  }
  return new Uint8Array(buf);
}

// ───────────────────────────── 3MF writer ─────────────────────────────
const xmlEsc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const hex8 = n => (n >>> 0).toString(16).padStart(8, '0');
const num = v => {                     // compact, locale-free number formatting
  const r = Math.round(v * 1e4) / 1e4;
  return Object.is(r, -0) ? '0' : String(r);
};
const MODEL_NS = 'unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" ' +
  'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"';
const APP_VERSION = '02.00.00.00';

function meshXML(mesh, cx, cy, cz, parts) {
  const P = mesh.pos, Tr = mesh.tri;
  const CH = 4096;
  let buf = '';
  const flush = () => { parts.push(buf); buf = ''; };
  buf += '   <mesh>\n    <vertices>\n';
  for (let i = 0; i < P.length; i += 3) {
    buf += `     <vertex x="${num(P[i] - cx)}" y="${num(P[i + 1] - cy)}" z="${num(P[i + 2] - cz)}"/>\n`;
    if ((i / 3) % CH === CH - 1) flush();
  }
  buf += '    </vertices>\n    <triangles>\n';
  for (let i = 0; i < Tr.length; i += 3) {
    buf += `     <triangle v1="${Tr[i]}" v2="${Tr[i + 1]}" v3="${Tr[i + 2]}"/>\n`;
    if ((i / 3) % CH === CH - 1) flush();
  }
  buf += '    </triangles>\n   </mesh>\n';
  flush();
}

// Returns an array of { name, data: Uint8Array } zip entries.
// colours: an array of filament colours ('#rrggbb'), one per filament (extruder 1, 2, …),
// or { light, dark } for two filaments.
export function build3MFEntries(plan, colours, title) {
  const fil = (Array.isArray(colours) ? colours : [colours.light, colours.dark]).map(c => c.toUpperCase());
  const nf = fil.length, rep = v => fil.map(() => v);
  const enc = new TextEncoder();
  const entries = [];
  const add = (name, str) => entries.push({ name, data: typeof str === 'string' ? enc.encode(str) : str });
  const { printer, plates, objects } = plan;
  const bed = printer.bed;

  // Object frame: centre on XY bounds and Z middle (as Bambu Studio does).
  for (const o of objects) {
    let mn = [Infinity, Infinity, Infinity], mx = [-Infinity, -Infinity, -Infinity];
    for (const v of o.volumes) {
      const b = v.mesh.bounds();
      for (let k = 0; k < 3; k++) { mn[k] = Math.min(mn[k], b.min[k]); mx[k] = Math.max(mx[k], b.max[k]); }
    }
    o.center = [(mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2];
    o.halfH = (mx[2] - mn[2]) / 2;
    o.zMin = mn[2];
  }

  // Ids: sub-objects (volumes) first, then the assembly object, like Bambu.
  let nextId = 1, fileNo = 0;
  const files = [];                       // { path, objs:[{id, mesh, center}] }
  const shared = new Map();               // piece type → { path, id }
  for (const o of objects) {
    o.parts = [];
    let file = null;
    for (const v of o.volumes) {
      if (v.shared && shared.has(v.shared)) { o.parts.push({ ...shared.get(v.shared), vol: v }); continue; }
      if (!file) { file = { path: `/3D/Objects/object_${++fileNo}.model`, objs: [] }; files.push(file); }
      const id = nextId++;
      file.objs.push({ id, mesh: v.mesh, center: o.center });
      const ref = { path: file.path, id };
      if (v.shared) shared.set(v.shared, ref);
      o.parts.push({ ...ref, vol: v });
    }
    o.id = nextId++;
  }

  add('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n' +
    ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n' +
    ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n' +
    ' <Default Extension="png" ContentType="image/png"/>\n' +
    ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>');
  add('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n' +
    ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n' +
    '</Relationships>');

  // sub-model files with the meshes
  for (const f of files) {
    const parts = [`<?xml version="1.0" encoding="UTF-8"?>\n<model ${MODEL_NS}>\n <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n`];
    for (const so of f.objs) {
      parts.push(`  <object id="${so.id}" p:UUID="${hex8(so.id << 16)}-81cb-4c03-9d28-80fed5dfa1dc" type="model">\n`);
      meshXML(so.mesh, so.center[0], so.center[1], so.center[2], parts);
      parts.push('  </object>\n');
    }
    parts.push(' </resources>\n <build/>\n</model>\n');
    entries.push({ name: f.path.slice(1), parts });
  }
  add('3D/_rels/3dmodel.model.rels', '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n' +
    files.map((f, i) => ` <Relationship Target="${f.path}" Id="rel-${i + 1}" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n`).join('') +
    '</Relationships>');

  // build items (world coordinates: each plate has its own origin)
  const items = [];
  const instCount = new Map();
  plates.forEach((pl, pi) => {
    const [ox, oy] = plateOrigin(pi, plates.length, bed);
    for (const it of pl.items) {
      const inst = instCount.get(it.obj) || 0;
      instCount.set(it.obj, inst + 1);
      items.push({ obj: it.obj, inst, plate: pi, x: ox + it.x, y: oy + it.y, z: it.obj.halfH });
    }
  });

  const date = new Date().toISOString().slice(0, 10);
  let root = `<?xml version="1.0" encoding="UTF-8"?>\n<model ${MODEL_NS}>\n` +
    ` <metadata name="Application">BambuStudio-${APP_VERSION}</metadata>\n` +
    ` <metadata name="BambuStudio:3mfVersion">1</metadata>\n` +
    ` <metadata name="Title">${xmlEsc(title)}</metadata>\n` +
    ` <metadata name="Description">Generated by Chess Set Generator</metadata>\n` +
    ` <metadata name="CreationDate">${date}</metadata>\n <metadata name="ModificationDate">${date}</metadata>\n <resources>\n`;
  objects.forEach((o, oi) => {
    root += `  <object id="${o.id}" p:UUID="${hex8(oi + 1)}-61cb-4c03-9d28-80fed5dfa1dc" type="model">\n   <components>\n`;
    o.parts.forEach((p, k) => {
      root += `    <component p:path="${p.path}" objectid="${p.id}" p:UUID="${hex8(((oi + 1) << 16) + k)}-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n`;
    });
    root += '   </components>\n  </object>\n';
  });
  root += ' </resources>\n <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">\n';
  items.forEach((it, i) => {
    root += `  <item objectid="${it.obj.id}" p:UUID="${hex8(i + 1)}-b1ec-4553-aec9-835e5b724bb4" transform="1 0 0 0 1 0 0 0 1 ${num(it.x)} ${num(it.y)} ${num(it.z)}" printable="1"/>\n`;
  });
  root += ' </build>\n</model>\n';
  add('3D/3dmodel.model', root);

  // model_settings.config: names, per-part filaments and plates
  const I = '1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1';
  let cfg = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n';
  for (const o of objects) {
    const faces = o.parts.reduce((a, p) => a + p.vol.mesh.triCount, 0);
    cfg += `  <object id="${o.id}">\n    <metadata key="name" value="${xmlEsc(o.name)}"/>\n    <metadata key="extruder" value="${o.extruder}"/>\n    <metadata face_count="${faces}"/>\n`;
    for (const p of o.parts) {
      cfg += `    <part id="${p.id}" subtype="normal_part">\n      <metadata key="name" value="${xmlEsc(p.vol.name)}"/>\n      <metadata key="matrix" value="${I}"/>\n` +
        `      <metadata key="extruder" value="${p.vol.extruder}"/>\n` +
        `      <mesh_stat face_count="${p.vol.mesh.triCount}" edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n    </part>\n`;
    }
    cfg += '  </object>\n';
  }
  let identify = 100;
  plates.forEach((pl, pi) => {
    cfg += `  <plate>\n    <metadata key="plater_id" value="${pi + 1}"/>\n    <metadata key="plater_name" value="${xmlEsc(pl.name)}"/>\n    <metadata key="locked" value="false"/>\n`;
    for (const it of items.filter(x => x.plate === pi)) {
      cfg += `    <model_instance>\n      <metadata key="object_id" value="${it.obj.id}"/>\n      <metadata key="instance_id" value="${it.inst}"/>\n      <metadata key="identify_id" value="${identify++}"/>\n    </model_instance>\n`;
    }
    cfg += '  </plate>\n';
  });
  cfg += '</config>\n';
  add('Metadata/model_settings.config', cfg);

  // project_settings.config: only names of Bambu system presets plus the
  // project-level values. Bambu Studio fills every other setting from the
  // named system presets (different_settings_to_system is empty).
  const towerX = plates.map(p => num(p.tower ? p.tower[0] : 15));
  const towerY = plates.map(p => num(p.tower ? p.tower[1] : 220));
  const project = {
    curr_bed_type: 'Textured PEI Plate',
    different_settings_to_system: new Array(nf + 2).fill(''),
    filament_colour: fil,
    filament_diameter: rep('1.75'),
    filament_settings_id: rep(printer.filament),
    filament_type: rep('PLA'),
    flush_multiplier: ['1'],
    // light → dark 280 mm³; dark → light 700 mm³ so white squares stay clean (2 filaments)
    flush_volumes_matrix: nf === 2 ? ['0', '280', '700', '0'] : fil.flatMap((_, i) => fil.map((_, j) => (i === j ? '0' : '280'))),
    flush_volumes_vector: new Array(nf * 2).fill('140'),
    from: 'project',
    inherits_group: new Array(nf + 2).fill(''),
    name: 'project_settings',
    nozzle_diameter: ['0.4'],
    print_settings_id: printer.process,
    printer_model: printer.model,
    printer_settings_id: printer.printer,
    printer_technology: 'FFF',
    version: APP_VERSION,
    wipe_tower_x: towerX,
    wipe_tower_y: towerY,
  };
  add('Metadata/project_settings.config', JSON.stringify(project, null, 4));
  return entries;
}

// ───────────────────────────── zip ─────────────────────────────
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
  return t;
})();
function crcUpdate(crc, u8) {
  let c = crc;
  for (let i = 0; i < u8.length; i++) c = CRC_TABLE[(c ^ u8[i]) & 0xFF] ^ (c >>> 8);
  return c;
}

async function deflateParts(chunks) {
  if (typeof CompressionStream !== 'function') return null;
  try {
    const cs = new CompressionStream('deflate-raw');
    const writer = cs.writable.getWriter();
    const out = [];
    const reading = (async () => {
      const reader = cs.readable.getReader();
      for (;;) { const { done, value } = await reader.read(); if (done) break; out.push(value); }
    })();
    for (const c of chunks) await writer.write(c);
    await writer.close();
    await reading;
    let n = 0; for (const c of out) n += c.length;
    const u8 = new Uint8Array(n); let o = 0; for (const c of out) { u8.set(c, o); o += c.length; }
    return u8;
  } catch (e) { return null; }
}

// entries: [{ name, data?: Uint8Array, parts?: string[] }]; returns Uint8Array[] parts of a zip
export async function makeZip(entries, onProgress) {
  const enc = new TextEncoder();
  const out = [], central = [];
  let offset = 0;
  const now = new Date();
  const dosTime = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
  const dosDate = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
  for (let ei = 0; ei < entries.length; ei++) {
    const e = entries[ei];
    const chunks = e.parts ? e.parts.map(s => enc.encode(s)) : [e.data];
    e.parts = null;
    let crc = 0xFFFFFFFF, size = 0;
    for (const c of chunks) { crc = crcUpdate(crc, c); size += c.length; }
    crc = (crc ^ 0xFFFFFFFF) >>> 0;
    let data = await deflateParts(chunks), method = 8;
    if (!data || data.length >= size) {
      method = 0;
      data = new Uint8Array(size); let o = 0; for (const c of chunks) { data.set(c, o); o += c.length; }
    }
    const nameBytes = enc.encode(e.name);
    const lh = new DataView(new ArrayBuffer(30));
    lh.setUint32(0, 0x04034b50, true); lh.setUint16(4, 20, true); lh.setUint16(6, 0, true);
    lh.setUint16(8, method, true); lh.setUint16(10, dosTime, true); lh.setUint16(12, dosDate, true);
    lh.setUint32(14, crc, true); lh.setUint32(18, data.length, true); lh.setUint32(22, size, true);
    lh.setUint16(26, nameBytes.length, true); lh.setUint16(28, 0, true);
    out.push(new Uint8Array(lh.buffer), nameBytes, data);
    const ch = new DataView(new ArrayBuffer(46));
    ch.setUint32(0, 0x02014b50, true); ch.setUint16(4, 20, true); ch.setUint16(6, 20, true);
    ch.setUint16(8, 0, true); ch.setUint16(10, method, true); ch.setUint16(12, dosTime, true);
    ch.setUint16(14, dosDate, true); ch.setUint32(16, crc, true); ch.setUint32(20, data.length, true);
    ch.setUint32(24, size, true); ch.setUint16(28, nameBytes.length, true);
    ch.setUint32(42, offset, true);
    central.push(new Uint8Array(ch.buffer), nameBytes);
    offset += 30 + nameBytes.length + data.length;
    if (onProgress) onProgress((ei + 1) / entries.length);
  }
  let csize = 0; for (const c of central) csize += c.length;
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, entries.length, true); end.setUint16(10, entries.length, true);
  end.setUint32(12, csize, true); end.setUint32(16, offset, true);
  return [...out, ...central, new Uint8Array(end.buffer)];
}

export async function build3MF(plan, colours, title, onProgress) {
  const entries = build3MFEntries(plan, colours, title);
  return makeZip(entries, onProgress);
}
