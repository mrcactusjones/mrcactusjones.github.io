# Chess Set Generator

Type in a board size and thickness and get a Bambu Studio project `.3mf`:

- a two-colour board: light filament body with the dark squares inlaid into the top layers;
- optionally split into four quarters that join with dovetails (one per plate);
- a full 32-piece set, uniformly scaled so the king's base is a set percentage of a square;
- plates, per-part filaments and the printer/process/filament presets already set.

Everything runs in the browser. Open `/chess-generator/` on the site.

## Files

- `index.html`: the page (inputs, 3D preview, plate thumbnails, download).
- `gen.js`: board geometry, piece scaling, plate layout and the 3MF/zip writer. No DOM, so it also runs in Node.
- `pieces/*.stl`: the piece set, in mm, standing on Z = 0, centred on the origin.
  They are the uploaded Signature-size pieces (4.40 in king) decimated to 30–50k triangles
  (max deviation ≤ 0.1 mm, still watertight). The knight is centred on its base and faces +Y.

## How the 3MF is put together

- Each board quarter is one object with two parts: `light squares` (filament 1: the slab plus the
  light tops) and `dark squares` (filament 2: the dark tops). Every square and dovetail tab is its
  own closed prism, so nothing overlaps.
- Tabs take the colour of the square they sit in, so the pattern is unbroken across the seams.
  Each quarter is shrunk by the joint clearance along its seams, so the gap is 2 × clearance.
- Pieces are one object per type and colour; extra copies are instances, and light and dark
  objects share one mesh.
- `Metadata/project_settings.config` only names Bambu system presets (printer, `0.20mm Standard`,
  `Bambu PLA Basic`) plus the filament colours, bed type and prime tower positions. Bambu Studio
  fills every other setting from those presets.
