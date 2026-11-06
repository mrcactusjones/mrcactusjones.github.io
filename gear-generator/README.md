# Gear Generator

Involute gears for 3D printing. It is the Toolkit's **Gear Generator** tab (`/#gears`).

- Spur, helical or herringbone teeth, sized by module, diametral pitch, circular pitch, or
  gear 1's outside diameter (the module is solved so the tips land exactly on that size),
  14.5° / 20° / 25° pressure angle.
- One gear by default; optionally a mating gear (centre distance worked out, profile shift
  included) or a rack (length in teeth, back height, mounting holes).
- Bores: round, D-shaft, hex, keyway (printed 0.15 mm oversize). Optional hub on one side.
- Filament-saving cut-outs: spoke windows, round holes or radial slots, with the saving shown.
- 3D preview; the parts don't spin until you press SPIN.
- Exports: STL (zipped when there are two parts), a Bambu Studio 3MF with the chosen printer
  preset, and SVG / DXF outlines for a laser or CAD.

## How the teeth are made

`gear.js` rolls the basic rack (20° flanks, 1.25 m dedendum, 0.38 m tip radius) around the
blank and keeps the envelope, the same way a hobbing cutter does. That gives a true involute,
the real trochoid root fillet, and correct undercut on small gears. Then:

- **Profile shift** is automatic by default: just enough to stop undercut. If that would make
  the teeth pointed, the tip circle is trimmed so every tooth keeps a 0.25 m flat.
- **Backlash** thins each tooth by half the total at the pitch circle.
- **Helical / herringbone**: the tooth outline twists with height (mating gear the opposite
  hand, rack sheared to match); bores and cut-outs stay straight.

The geometry was checked numerically: tooth thickness matches theory to ~1 µm, flanks sit on
the analytic involute, every mesh is watertight, and meshing pairs (spur, helical, herringbone,
gear + rack) have zero overlap through a full tooth cycle with a constant flank gap equal to the
backlash.

## Files

- `gear.js`: geometry, meshes, readouts, SVG/DXF, 3MF plate layout. No DOM; runs in Node.
  Cap triangulation is passed in (the page wraps `THREE.ShapeUtils.triangulateShape`).
- `../lib/print3d.js`: shared with the chess generator: meshes, STL, Bambu printer presets,
  the Bambu Studio 3MF writer and zip packer.
