#!/usr/bin/env python3
"""
Signature Chess Set — parametric generator
==========================================

Generates the six pieces of the set as watertight binary STL files.

    King height ........ 4.4 in  (111.76 mm)
    King base diameter . 1.8 in  ( 45.72 mm)

Every piece is built from a shared "family" lower body (stepped base, belly
bulb, stacked beads, shoulder collars) so the set reads as one design, with a
piece-specific top:

    pawn   - collar + ball
    rook   - tapered tower, dished top, 6 crenellations
    bishop - fluted onion mitre with a slanted slit and apex ball
    queen  - scalloped 8-petal coronet with inner dome and apex ball
    king   - large ball, bead stack, twisted 8-flute flame finial
    knight - lofted horse head with a swept mane on the family plinth

Dependencies:  pip install numpy trimesh manifold3d

Usage:
    python3 generate_chess_set.py            # writes ./stl/*.stl + zip
    python3 generate_chess_set.py king pawn  # only the named pieces
    python3 generate_chess_set.py --fast     # coarse meshes for previewing
"""
import math
import os
import sys
import zipfile

import numpy as np
import manifold3d as mf
import trimesh

IN = 25.4
KING_H = 4.4 * IN          # 111.76 mm
KING_BASE_D = 1.8 * IN     # 45.72 mm

# Piece dimensions (mm).  Heights/bases follow the reference set's proportions.
DIMS = {
    #          height   base diameter
    "pawn":   (62.0,    30.0),
    "rook":   (70.0,    39.0),
    "knight": (78.0,    41.0),
    "bishop": (83.0,    38.0),
    "queen":  (93.0,    41.0),
    "king":   (KING_H,  KING_BASE_D),
}

FAST = "--fast" in sys.argv
N_THETA = 96 if FAST else 128          # segments around the axis (chord error < 0.01 mm)
CURVE_N = 8 if FAST else 13            # samples per bezier segment
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "stl")

K = 0.5522847498  # bezier quarter-circle handle length


# ----------------------------------------------------------------------------
# 2D lathe profile builder  (r = radius, z = height)
# ----------------------------------------------------------------------------
class Profile:
    """Poly-line/bezier path in the (r, z) half-plane, traversed bottom->top.

    The path must start and end on the axis (r = 0).  Material lies between
    the axis and the path, so traversing 'up' keeps the material on the left.
    """

    def __init__(self, z0=0.0):
        self.pts = [(0.0, float(z0))]

    # -- primitives --------------------------------------------------------
    @property
    def cur(self):
        return self.pts[-1]

    def line(self, r, z):
        self.pts.append((float(r), float(z)))
        return self

    def bez(self, c1, c2, end, n=None):
        n = n or CURVE_N
        p0, p1, p2, p3 = (np.array(p, float) for p in (self.cur, c1, c2, end))
        t = np.linspace(0.0, 1.0, n + 1)[1:, None]
        pts = ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * p1 \
            + 3 * (1 - t) * (t ** 2) * p2 + (t ** 3) * p3
        self.pts.extend(map(tuple, pts))
        return self

    def qell(self, end, start_dir, n=None):
        """Quarter ellipse from the current point to `end`.

        start_dir='r': leave moving radially, arrive moving vertically
                       (convex belly when moving outward, cove when inward)
        start_dir='z': leave moving vertically, arrive moving radially
                       (convex shoulder when moving inward)
        """
        r0, z0 = self.cur
        r1, z1 = end
        if start_dir == "r":
            c1 = (r0 + K * (r1 - r0), z0)
            c2 = (r1, z1 - K * (z1 - z0))
        else:
            c1 = (r0, z0 + K * (z1 - z0))
            c2 = (r1 - K * (r1 - r0), z1)
        return self.bez(c1, c2, end, n)

    # -- turned features -----------------------------------------------------
    def disc(self, r, z_top, f_bot=0.0, f_top=0.0):
        """Flat collar of radius r rising from the current z to z_top, with
        optional rounded (fillet) outer edges.  Ends at (r - f_top, z_top)."""
        r0, z0 = self.cur
        if f_bot > 0:
            self.line(r - f_bot, z0).qell((r, z0 + f_bot), "r")
        else:
            self.line(r, z0)
        if f_top > 0:
            self.line(r, z_top - f_top).qell((r - f_top, z_top), "z")
        else:
            self.line(r, z_top)
        return self

    def bead(self, z_top, r_max, r_top, frac=0.45, neck=0.0):
        """Bulb from the current neck point up to (r_top, z_top)."""
        r0, z0 = self.cur
        if neck > 0:
            self.line(r0, z0 + neck)
            z0 += neck
        zm = z0 + frac * (z_top - z0)
        self.qell((r_max, zm), "r").qell((r_top, z_top), "z")
        return self

    def vgroove(self, depth, width):
        r0, z0 = self.cur
        self.line(r0 - depth, z0 + width * 0.5).line(r0, z0 + width)
        return self

    def close(self):
        r, z = self.cur
        if r != 0.0:
            self.line(0.0, z)
        return np.array(self.pts, float)


def dedupe(pts):
    keep = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - keep[-1][0]) > 1e-7 or abs(p[1] - keep[-1][1]) > 1e-7:
            keep.append(p)
    return np.array(keep)


# ----------------------------------------------------------------------------
# Meshing
# ----------------------------------------------------------------------------
def revolve_fn(profile_fn, n_theta=None):
    """Revolve a theta-dependent profile.  profile_fn(theta) -> (N,2) array of
    (r, z) whose first and last points lie on the axis.  N must not vary."""
    n_theta = n_theta or N_THETA
    thetas = np.linspace(0.0, 2.0 * math.pi, n_theta, endpoint=False)
    profs = np.stack([np.asarray(profile_fn(t), float) for t in thetas])  # T,N,2
    T, N, _ = profs.shape
    assert N >= 3
    r = profs[:, 1:-1, 0]
    z = profs[:, 1:-1, 1]
    M = N - 2
    x = r * np.cos(thetas)[:, None]
    y = r * np.sin(thetas)[:, None]
    V = np.stack([x.ravel(), y.ravel(), z.ravel()], axis=1)
    z_bot = profs[:, 0, 1].mean()
    z_top = profs[:, -1, 1].mean()
    V = np.vstack([V, [[0, 0, z_bot], [0, 0, z_top]]])
    pb = T * M
    pt = pb + 1

    def idx(t, i):
        return (t % T) * M + i

    F = []
    tt = np.arange(T)
    t2 = (tt + 1) % T
    for i in range(M - 1):
        a = idx(tt, i)
        b = idx(t2, i)
        c = idx(t2, i + 1)
        d = idx(tt, i + 1)
        F.append(np.stack([a, b, c], 1))
        F.append(np.stack([a, c, d], 1))
    F.append(np.stack([np.full(T, pb), idx(t2, 0), idx(tt, 0)], 1))
    F.append(np.stack([np.full(T, pt), idx(tt, M - 1), idx(t2, M - 1)], 1))
    F = np.vstack(F)
    return to_manifold(V, F)


def revolve(profile_pts, n_theta=None):
    pts = dedupe(np.asarray(profile_pts, float))
    return revolve_fn(lambda th: pts, n_theta)


def to_manifold(V, F):
    m = mf.Manifold(mf.Mesh(np.ascontiguousarray(V, dtype=np.float32),
                            np.ascontiguousarray(F, dtype=np.uint32)))
    if m.status() != mf.Error.NoError:
        raise RuntimeError(f"mesh not manifold: {m.status()}")
    if m.volume() < 0:
        m = mf.Manifold(mf.Mesh(np.ascontiguousarray(V, dtype=np.float32),
                                np.ascontiguousarray(F[:, ::-1], dtype=np.uint32)))
    return m


def to_trimesh(m):
    mesh = m.to_mesh()
    return trimesh.Trimesh(np.asarray(mesh.vert_properties[:, :3], float),
                           np.asarray(mesh.tri_verts, np.int64), process=False)


def sphere(r, center=(0, 0, 0), n=None):
    return mf.Manifold.sphere(r, n or N_THETA).translate(list(center))


def box(sx, sy, sz, center=(0, 0, 0)):
    return mf.Manifold.cube([sx, sy, sz], True).translate(list(center))


def cyl(h, r, center=(0, 0, 0), n=None):
    return mf.Manifold.cylinder(h, r, r, n or N_THETA).translate(list(center))


# ----------------------------------------------------------------------------
# Family lower body (shared by every piece)
# ----------------------------------------------------------------------------
def base_heights(R):
    """Foot disc and second disc heights.  Mostly absolute so the pawn's base
    is nearly as tall as the king's (as on the reference set)."""
    hf = 0.09 * R + 2.9
    h2 = 0.07 * R + 2.1
    return hf, h2


def family_base(P, R):
    """Stepped foot: wide bull-nosed disc, groove, second rounded disc.
    Returns z at the top of the second disc."""
    hf, h2 = base_heights(R)
    ft = 0.52 * hf
    P.line(R - 0.7, 0.0).line(R, 0.7)                 # small bottom chamfer
    P.line(R, hf - ft).qell((R - ft, hf), "z")         # foot disc, bull-nosed
    R2 = 0.865 * R
    f2 = 0.50 * h2
    P.line(R2 + 0.3, hf).line(R2 - 0.35, hf + 0.45).line(R2, hf + 0.9)   # undercut groove
    P.line(R2, hf + h2 - f2).qell((R2 - f2, hf + h2), "z")
    return hf + h2


def family_belly(P, R, z0, height, r_max=0.69, r_top=0.40, r_neck=0.64, frac=0.28):
    """Squat cushion bulb seated on the second disc (radii as fractions of R)."""
    P.line(r_neck * R, z0)
    P.bead(z0 + height, r_max * R, r_top * R, frac=frac)
    return z0 + height


def family_beads(P, z0, z1, radii, top_ratio=0.70, frac=0.42, neck=0.35):
    """Stack of oblate beads filling z0..z1 with equal heights."""
    n = len(radii)
    h = (z1 - z0) / n
    z = z0
    for i, rm in enumerate(radii):
        P.bead(z + h, rm, top_ratio * rm, frac=frac, neck=neck if i else 0.0)
        z += h
    return z


def shoulder_height(R, h_low=0.33, h_high=0.11, gap=0.6):
    return h_low * R + gap + h_high * R


def family_shoulders(P, R, z0, r_low=0.61, h_low=0.33, r_high=0.52, h_high=0.11, gap=0.6):
    """Thick lower collar disc + thin upper flange (fractions of R)."""
    hl = h_low * R
    hh = h_high * R
    f1 = 0.46 * hl
    P.disc(r_low * R, z0 + hl, f_bot=f1, f_top=f1)
    z = z0 + hl
    rg = 0.78 * r_high * R
    P.line(rg, z).line(rg, z + gap)
    z += gap
    f2 = 0.48 * hh
    P.disc(r_high * R, z + hh, f_bot=f2, f_top=f2)
    return z + hh


def lower_body(P, R, z_shoulder_top, bead_radii, belly_h, shoulders=None, belly=None):
    """Base + belly + beads + shoulders, ending exactly at z_shoulder_top.
    Returns the z reached (== z_shoulder_top)."""
    shoulders = shoulders or {}
    belly = belly or {}
    z = family_base(P, R)
    z = family_belly(P, R, z, belly_h, **belly)
    sh = shoulder_height(R, shoulders.get("h_low", 0.33), shoulders.get("h_high", 0.11), shoulders.get("gap", 0.6))
    z = family_beads(P, z, z_shoulder_top - sh, bead_radii)
    z = family_shoulders(P, R, z, **shoulders)
    return z


# ----------------------------------------------------------------------------
# Pieces
# ----------------------------------------------------------------------------
def make_pawn():
    H, D = DIMS["pawn"]
    R = D / 2
    rb = 0.58 * R                      # ball radius
    rn = 0.30 * R                      # neck radius
    neck_h = 0.09 * R
    hc = 0.22 * R                      # collar height
    rc = 0.62 * R                      # collar radius
    zc_ball = H - rb
    z_neck_top = zc_ball - math.sqrt(rb * rb - rn * rn)
    z_collar_top = z_neck_top - neck_h
    z_collar_bot = z_collar_top - hc

    P = Profile()
    z = family_base(P, R)
    z = family_belly(P, R, z, 0.64 * R)
    z = family_beads(P, z, z_collar_bot, [0.44 * R, 0.39 * R])
    P.disc(rc, z_collar_top, f_bot=0.46 * hc, f_top=0.46 * hc)
    P.line(rn, z_collar_top).line(rn, z_neck_top + 0.3)
    body = revolve(P.close())
    return body + sphere(rb, (0, 0, zc_ball))


def make_rook():
    H, D = DIMS["rook"]
    R = D / 2
    tower_h = 0.255 * H
    z_t0 = H - tower_h
    P = Profile()
    lower_body(P, R, z_t0, [0.49 * R, 0.43 * R], belly_h=0.62 * R,
               shoulders=dict(r_low=0.56, h_low=0.24, r_high=0.66, h_high=0.11))
    # tower: gentle entasis taper, cavetto flare to a crisp rim
    r_base = 0.62 * R
    r_waist = 0.585 * R
    r_rim = 0.70 * R
    rim_h = 0.25 * R
    fr = 0.8
    z = z_t0
    P.line(r_base, z).line(r_base, z + 0.4)
    z_w = H - rim_h
    P.bez((r_base, z + 0.45 * (z_w - z)), (r_waist, z_w - 0.25 * (z_w - z)), (r_waist, z_w))
    P.bez((r_waist, z_w + 0.45 * rim_h), (r_rim - 0.6, z_w + 0.62 * rim_h), (r_rim, H - fr - 0.3))
    P.line(r_rim, H - fr).qell((r_rim - fr, H), "z")
    body = revolve(P.close())
    # dished top + six crenellations (one facing -y, toward the viewer)
    wall = 0.14 * R
    depth = 0.26 * R
    body = body - cyl(depth + 1, r_rim - wall, (0, 0, H - depth))
    notch_w = 0.17 * R
    for k in range(6):
        b = box(r_rim + 2, notch_w, depth + 1, (r_rim / 2 + 1, 0, H - depth / 2 + 0.5))
        body = body - b.rotate([0, 0, 30 + 60 * k])
    return body


def make_bishop():
    H, D = DIMS["bishop"]
    R = D / 2
    ball_r = 0.125 * R
    neck_r = 0.085 * R
    neck_h = 0.05 * R
    mitre_h = 0.235 * H
    z_ball_c = H - ball_r
    z_mitre_top = z_ball_c - math.sqrt(ball_r ** 2 - neck_r ** 2) - neck_h
    z0 = z_mitre_top - mitre_h

    P = Profile()
    lower_body(P, R, z0, [0.48 * R, 0.43 * R], belly_h=0.64 * R,
               shoulders=dict(r_low=0.58, h_low=0.27, r_high=0.50, h_high=0.11))
    # mitre: fat onion with an ogee flame taper
    h = mitre_h
    r_base = 0.37 * R
    r_max = 0.53 * R
    P.line(r_base, z0)
    P.bez((r_max * 1.06, z0 + 0.05 * h), (r_max * 1.05, z0 + 0.30 * h), (r_max * 0.90, z0 + 0.52 * h))
    P.bez((r_max * 0.60, z0 + 0.70 * h), (neck_r * 1.5, z0 + 0.86 * h), (neck_r, z_mitre_top))
    P.line(neck_r, z_mitre_top + neck_h + 0.3)
    body = revolve(P.close()) + sphere(ball_r, (0, 0, z_ball_c))
    # slanted slit: a half-slab that starts inside the mitre and leans out
    # through the right shoulder of the onion; runs front-to-back (along y)
    # so the cut is seen face-on from the front.
    tilt = 30.0
    slit_w = 2.6
    slab = mf.Manifold.cube([slit_w, 60.0, 40.0], True).translate([0, 0, 20.0])
    slab = slab.rotate([0, tilt, 0]).translate([-0.09 * R, 0, z0 + 0.42 * h])
    return body - slab


def petal(theta, n=8, sharp=0.75):
    """0..1 scallop shape with rounded tips and pointed valleys."""
    return abs(math.cos(n * theta / 2.0)) ** sharp


def make_queen():
    H, D = DIMS["queen"]
    R = D / 2
    ball_r = 0.11 * R
    dome_r = 0.40 * R
    peek = 0.26 * R                    # dome rises this far above the petal valleys
    cup_h = 0.62 * R
    amp = 0.10 * R
    z_ball_c = H - ball_r
    z_dome_top = z_ball_c - ball_r * 0.55
    dome_c = z_dome_top - dome_r
    z_valley = z_dome_top - peek
    z_cup0 = z_valley + amp - cup_h
    neck_h = 0.05 * R
    z_sh = z_cup0 - neck_h

    P = Profile()
    lower_body(P, R, z_sh, [0.49 * R, 0.45 * R, 0.41 * R], belly_h=0.64 * R,
               shoulders=dict(r_low=0.60, h_low=0.30, r_high=0.52, h_high=0.11))
    r_neck = 0.36 * R
    P.line(r_neck, z_sh).line(r_neck, z_cup0 + 0.3)
    body = revolve(P.close())

    r_bot = 0.40 * R
    r_rim = 0.64 * R
    wall = 0.115 * R
    floor_z = z_cup0 + 0.20 * R
    fillet = 0.5 * wall

    def cup_profile(theta):
        zr = z_valley + amp * petal(theta, 8, 0.75)
        Q = Profile(z_cup0)
        Q.line(r_bot, z_cup0)
        Q.bez((r_bot * 1.01, z_cup0 + 0.40 * (zr - z_cup0)), (r_rim * 0.90, z_cup0 + 0.74 * (zr - z_cup0)),
              (r_rim, zr - fillet))
        Q.qell((r_rim - fillet, zr), "z")
        Q.line(r_rim - wall + fillet, zr)
        Q.qell((r_rim - wall, zr - fillet), "r")
        Q.bez((r_rim * 0.88 - wall, z_cup0 + 0.72 * (zr - z_cup0)), (r_bot * 0.80, floor_z + 0.25 * (zr - floor_z)),
              (r_bot * 0.65, floor_z))
        return Q.close()

    cup = revolve_fn(cup_profile)
    dome = sphere(dome_r, (0, 0, dome_c))
    stem = cyl(z_ball_c - z_dome_top + 1.0, 0.05 * R, (0, 0, z_dome_top - 0.5))
    ball = sphere(ball_r, (0, 0, z_ball_c))
    return body + cup + dome + stem + ball


def make_king():
    H, D = DIMS["king"]
    R = D / 2
    ball_r = 0.53 * R
    r_neck = 0.32 * R
    neck_h = 0.06 * R
    r_stem = 0.15 * R
    finial_h = 0.155 * H
    z_f0 = H - finial_h
    # bead stack between ball and finial
    hc = 0.09 * R
    stack_h = 0.35 + hc + 0.03 * R + 0.15 * R + 0.03 * R
    z_join = z_f0 - stack_h
    z_ball_c = z_join - math.sqrt(ball_r ** 2 - r_stem ** 2)
    z_neck_top = z_ball_c - math.sqrt(ball_r ** 2 - r_neck ** 2)
    z_sh = z_neck_top - neck_h

    P = Profile()
    lower_body(P, R, z_sh, [0.49 * R, 0.45 * R, 0.41 * R], belly_h=0.64 * R,
               shoulders=dict(r_low=0.61, h_low=0.33, r_high=0.52, h_high=0.11))
    P.line(r_neck, z_sh).line(r_neck, z_neck_top + 0.3)
    body = revolve(P.close())
    ball = sphere(ball_r, (0, 0, z_ball_c))

    S = Profile(z_join - 1.0)
    S.line(r_stem, z_join - 1.0).line(r_stem, z_join + 0.35)
    zs = z_join + 0.35
    S.disc(0.20 * R, zs + hc, f_bot=0.38 * hc, f_top=0.38 * hc)
    zs += hc
    S.line(0.13 * R, zs).line(0.13 * R, zs + 0.03 * R)
    zs += 0.03 * R
    S.bead(zs + 0.15 * R, 0.19 * R, 0.115 * R, frac=0.5)
    zs += 0.15 * R
    r_fneck = 0.115 * R
    S.line(r_fneck, zs + 0.03 * R + 0.3)
    stack = revolve(S.close())

    # twisted, fluted flame finial
    hf = H - z_f0
    r_f_max = 0.215 * R
    n_flutes = 8
    twist = math.radians(55.0)

    def finial_profile(theta):
        Q = Profile(z_f0 - 0.3)
        Q.line(r_fneck, z_f0 - 0.3)
        Q.bez((r_f_max * 1.08, z_f0 + 0.10 * hf), (r_f_max * 1.10, z_f0 + 0.30 * hf), (r_f_max * 0.92, z_f0 + 0.50 * hf))
        Q.bez((r_f_max * 0.60, z_f0 + 0.68 * hf), (0.04 * R, z_f0 + 0.86 * hf), (0.0, H))
        pts = Q.close()
        r = pts[:, 0].copy()
        u = np.clip((pts[:, 1] - z_f0) / hf, 0, 1)
        depth = 0.30 * np.sin(math.pi * u) ** 0.9
        phi = theta + twist * u
        g = (0.5 + 0.5 * np.cos(n_flutes * phi)) ** 1.6
        pts[:, 0] = r * (1.0 - depth * g)
        return pts

    finial = revolve_fn(finial_profile)
    return body + ball + stack + finial


# ----------------------------------------------------------------------------
# Knight
# ----------------------------------------------------------------------------
def make_knight():
    from knight import build_knight_head
    H, D = DIMS["knight"]
    R = D / 2
    # plinth: family base + a drum with a rounded top edge
    P = Profile()
    z = family_base(P, R)
    r_drum = 0.58 * R
    z_drum_top = z + 0.56 * R
    fd = 0.09 * R
    P.line(r_drum + 0.3, z).line(r_drum, z + 0.4)
    P.line(r_drum, z_drum_top - fd).qell((r_drum - fd, z_drum_top), "z")
    plinth = revolve(P.close())
    V, Fc = build_knight_head(R, z_drum_top, H, voxel=0.45 if FAST else 0.28)
    # fit the highest lock exactly to H (z-only, a few percent at most)
    zmax = V[:, 2].max()
    V[:, 2] = z_drum_top + (V[:, 2] - z_drum_top) * (H - z_drum_top) / (zmax - z_drum_top)
    head = to_manifold(V, Fc)
    head = head.simplify(0.02 if not FAST else 0.03)
    # trim anything that dipped below the drum top so the union is clean
    head = head - box(200, 200, 40, (0, 0, z_drum_top - 0.5 - 20))
    return plinth + head


BUILDERS = {
    "pawn": make_pawn,
    "rook": make_rook,
    "bishop": make_bishop,
    "queen": make_queen,
    "king": make_king,
    "knight": make_knight,
}


def export(name, m):
    if not FAST:
        m = m.simplify(0.008)        # drop redundant triangles on flat/near-flat areas (8 um tolerance)
    tm = to_trimesh(m)
    assert tm.is_watertight, f"{name}: not watertight"
    zmin, zmax = tm.bounds[0][2], tm.bounds[1][2]
    d = 2 * max(np.abs(tm.vertices[:, :2]).max(axis=0))
    path = os.path.join(OUT_DIR, f"{name}.stl")
    tm.export(path)
    print(f"{name:7s}  height {zmax - zmin:7.2f} mm  base~{d:6.2f} mm  "
          f"tris {len(tm.faces):7d}  vol {tm.volume / 1000:6.1f} cm3  -> {os.path.relpath(path)}")
    return path


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    names = [a for a in sys.argv[1:] if not a.startswith("--")] or list(BUILDERS)
    paths = []
    for n in names:
        paths.append(export(n, BUILDERS[n]()))
    if set(names) == set(BUILDERS) and not FAST:
        zpath = os.path.join(OUT_DIR, "signature-chess-set-stl.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                zf.write(p, os.path.basename(p))
        print("zip ->", os.path.relpath(zpath))


if __name__ == "__main__":
    main()
