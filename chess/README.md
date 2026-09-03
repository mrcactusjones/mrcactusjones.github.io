# Signature Chess Set

A 3D-printable, Staunton-style chess set generated parametrically.
Everything in this folder is produced by `generate_chess_set.py`.

| Piece  | Height            | Base diameter     |
|--------|-------------------|-------------------|
| King   | 4.40 in / 111.8 mm | 1.80 in / 45.7 mm |
| Queen  | 3.66 in / 93.0 mm  | 1.61 in / 41.0 mm |
| Bishop | 3.27 in / 83.0 mm  | 1.50 in / 38.0 mm |
| Knight | 3.07 in / 78.0 mm  | 1.61 in / 41.0 mm |
| Rook   | 2.76 in / 70.0 mm  | 1.54 in / 39.0 mm |
| Pawn   | 2.44 in / 62.0 mm  | 1.18 in / 30.0 mm |

A full set is 32 pieces: 2 kings, 2 queens, 4 bishops, 4 knights, 4 rooks, 16 pawns.
A 1.8 in king base suits 2.25–2.375 in squares.

## Files

- `stl/*.stl` — one watertight binary STL per piece, in millimetres, standing on Z = 0.
- `stl/signature-chess-set-stl.zip` — all six STLs.
- `generate_chess_set.py` — the generator (turned pieces as lathe profiles + booleans).
- `knight.py` — the knight head sculpt (signed-distance field → marching cubes).
- `img/` — lineup renders (boxwood and rosewood previews); `lineup.png` is the website fallback image.

## Printing

- Import at 100 % (files are in mm). Print upright exactly as exported.
- FDM (0.4 mm nozzle): 0.12–0.16 mm layers, 3 walls, 15–25 % gyroid infill.
  Use tree/organic supports: the collar undersides, the bishop's slit and the
  knight's mane overhang. Tree supports leave the turned surfaces clean.
- Resin: supports on the same features; hollowing is optional — solid pieces
  have a better hand feel, or hollow and pour in steel shot before sealing.
- Print the two colours in contrasting filaments (e.g. a warm ivory/boxwood and a
  dark rosewood/ebony). Glue felt discs to the bases.

## Regenerating

```bash
pip install numpy trimesh manifold3d scikit-image
python3 generate_chess_set.py            # all pieces + zip
python3 generate_chess_set.py king pawn  # selected pieces only
python3 generate_chess_set.py --fast     # coarse, quick previews
```

Every dimension is a fraction of the piece's base radius, so changing a row in
`DIMS` rescales that piece while keeping the family look. Reference king:
4.4 in tall, 1.8 in base.
