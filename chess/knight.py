"""
Knight head sculpt for the Signature Chess Set.

The horse head is modelled as a signed-distance field: ellipsoids for the
skull, jaw, muzzle and neck, capsules for the ears and swept tapered strands
for the wind-blown mane, all blended with a smooth minimum so the result reads
as one carved form.  The field is sampled on a voxel grid and turned into a
mesh with marching cubes.

Coordinates (mm): the piece stands on z = 0, the horse faces -x, +y is the
horse's left.  All numbers are expressed in units of the knight's base radius
R so the head scales with the set.
"""
import math

import numpy as np
from skimage import measure


# ----------------------------------------------------------------------------
# SDF helpers (vectorised on numpy grids)
# ----------------------------------------------------------------------------
def smin(a, b, k):
    """Polynomial smooth minimum (Inigo Quilez)."""
    if k <= 0:
        return np.minimum(a, b)
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b * (1 - h) + a * h - k * h * (1.0 - h)


def smax(a, b, k):
    return -smin(-a, -b, k)


def rot_y(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


class Field:
    """Voxel grid holding the running SDF; primitives update only the block of
    voxels they can influence."""

    def __init__(self, lo, hi, voxel):
        self.lo = np.array(lo, float)
        self.voxel = voxel
        self.shape = tuple(int(math.ceil((h - l) / voxel)) + 1 for l, h in zip(lo, hi))
        self.xs = [self.lo[i] + np.arange(self.shape[i]) * voxel for i in range(3)]
        self.d = np.full(self.shape, 1e9, np.float32)

    def _block(self, bmin, bmax, margin):
        i0 = [max(0, int((bmin[i] - margin - self.lo[i]) / self.voxel)) for i in range(3)]
        i1 = [min(self.shape[i], int((bmax[i] + margin - self.lo[i]) / self.voxel) + 2) for i in range(3)]
        if any(i1[i] <= i0[i] for i in range(3)):
            return None
        sl = tuple(slice(i0[i], i1[i]) for i in range(3))
        X, Y, Z = np.meshgrid(self.xs[0][sl[0]], self.xs[1][sl[1]], self.xs[2][sl[2]], indexing="ij")
        return sl, np.stack([X, Y, Z], axis=-1)

    def _apply(self, sl, prim_d, k, mode):
        cur = self.d[sl]
        if mode == "add":
            self.d[sl] = smin(cur, prim_d, k)
        else:  # subtract
            self.d[sl] = smax(cur, -prim_d, k)

    # -- primitives ---------------------------------------------------------
    def ellipsoid(self, center, radii, rot_deg=0.0, k=0.0, mode="add"):
        center = np.array(center, float)
        radii = np.array(radii, float)
        Rm = rot_y(rot_deg)
        ext = np.abs(Rm) @ radii            # AABB half-extents of the rotated ellipsoid
        blk = self._block(center - ext, center + ext, k + 1.0)
        if blk is None:
            return
        sl, P = blk
        q = (P - center) @ Rm               # into the ellipsoid's local frame
        k0 = np.linalg.norm(q / radii, axis=-1)
        k1 = np.linalg.norm(q / (radii * radii), axis=-1)
        d = k0 * (k0 - 1.0) / np.maximum(k1, 1e-9)
        self._apply(sl, d.astype(np.float32), k, mode)

    def sphere(self, center, r, k=0.0, mode="add"):
        self.ellipsoid(center, (r, r, r), 0.0, k, mode)

    def capsule(self, a, b, ra, rb, k=0.0, mode="add", flat=1.0):
        """Round cone between a (radius ra) and b (radius rb).  flat > 1
        widens the section along y (flat ribbon-like locks)."""
        a = np.array(a, float)
        b = np.array(b, float)
        rmax = max(ra, rb) * max(flat, 1.0)
        lo = np.minimum(a, b) - rmax
        hi = np.maximum(a, b) + rmax
        blk = self._block(lo, hi, k + 1.0)
        if blk is None:
            return
        sl, P = blk
        ab = b - a
        L2 = ab @ ab
        t = np.clip(((P - a) @ ab) / L2, 0.0, 1.0)
        closest = a + t[..., None] * ab
        r = ra + (rb - ra) * t
        diff = P - closest
        if flat != 1.0:
            diff = diff * np.array([1.0, 1.0 / flat, 1.0])
        d = np.linalg.norm(diff, axis=-1) - r
        self._apply(sl, d.astype(np.float32), k, mode)

    def strand(self, pts, radii, k=0.0, mode="add", flat=1.0):
        """Swept tapered tube along a polyline of sample points."""
        for i in range(len(pts) - 1):
            self.capsule(pts[i], pts[i + 1], radii[i], radii[i + 1], k, mode, flat)

    # -- extraction -----------------------------------------------------------
    def mesh(self):
        # force the grid boundary outside the solid so the surface is closed
        d = self.d
        d[0, :, :] = d[-1, :, :] = 1.0
        d[:, 0, :] = d[:, -1, :] = 1.0
        d[:, :, 0] = d[:, :, -1] = 1.0
        verts, faces, _, _ = measure.marching_cubes(d, level=0.0, spacing=(self.voxel,) * 3,
                                                    allow_degenerate=False)
        verts = verts + self.lo
        import trimesh
        tm = trimesh.Trimesh(verts, faces[:, ::-1], process=True)
        tm.update_faces(tm.nondegenerate_faces())
        tm.update_faces(tm.unique_faces())
        tm.remove_unreferenced_vertices()
        parts = tm.split(only_watertight=False)
        if len(parts) > 1:
            tm = max(parts, key=lambda p: len(p.faces))
        if not tm.is_winding_consistent:
            tm.fix_normals()
        return np.asarray(tm.vertices), np.asarray(tm.faces)


def bezier(p0, p1, p2, p3, n):
    t = np.linspace(0, 1, n)[:, None]
    p0, p1, p2, p3 = (np.array(p, float) for p in (p0, p1, p2, p3))
    return ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * p1 + 3 * (1 - t) * (t ** 2) * p2 + (t ** 3) * p3


def catmull_rom(pts, n_per=8):
    pts = np.array(pts, float)
    P = np.vstack([pts[:1], pts, pts[-1:]])
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        t = np.linspace(0, 1, n_per, endpoint=(i == len(P) - 3))[:, None]
        out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t ** 2
                          + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3))
    return np.vstack(out)


# ----------------------------------------------------------------------------
# The horse
# ----------------------------------------------------------------------------
def build_knight_head(R, z_drum_top, H, voxel=0.30):
    """Return (verts, faces) of the head + neck + mane in mm.

    R          knight base radius (mm) -- everything scales with it
    z_drum_top top of the plinth drum the neck rises from
    H          total piece height (the mane's highest lock lands just under it)
    """
    u = R / 20.5                       # 1.0 for the default 41 mm base
    zd = z_drum_top

    # Neck centre-line: rises out of the drum, leans slightly back, arrives at the poll.
    neck_ctrl = [(1.0 * u, 0, zd - 4.0),
                 (1.8 * u, 0, zd + 10.0 * u),
                 (0.6 * u, 0, zd + 24.0 * u),
                 (-2.4 * u, 0, zd + 36.0 * u),
                 (-3.6 * u, 0, zd + 42.5 * u)]
    neck = catmull_rom(neck_ctrl, 7)
    n_neck = len(neck)

    def neck_radii(s):
        return (10.8 - 3.0 * s) * u, (8.2 - 2.4 * s) * u     # fore-aft, lateral

    # Head axis: from the poll down-forward to the nose (face ~22 deg from vertical).
    poll = np.array([-3.5 * u, 0, zd + 43.5 * u])
    nose = np.array([-17.5 * u, 0, zd + 9.5 * u])
    axis = nose - poll
    L = np.linalg.norm(axis)
    ax = axis / L
    tilt = math.degrees(math.atan2(ax[0], -ax[2]))
    fwd = np.array([-ax[2], 0, ax[0]])
    if fwd[0] > 0:
        fwd = -fwd

    def hp(t, depth=0.0, lateral=0.0):
        """Point near the head axis: t along, depth toward the face (+) or jaw (-), lateral +y."""
        return poll + ax * (t * L) + fwd * depth + np.array([0, lateral, 0])

    lo = (-30 * u, -15 * u, zd - 6)
    hi = (30 * u, 15 * u, H + 3)
    F = Field(lo, hi, voxel)
    rot = -tilt

    # --- neck: swept ellipse -----------------------------------------------------
    for i, c in enumerate(neck):
        a, b = neck_radii(i / (n_neck - 1))
        F.ellipsoid(c, (a, b, 5.0 * u), 0.0, 2.6 * u)

    # --- head: smooth chain of ellipsoids (along, lateral, depth) ------------------
    chain = [  # t,   along, lateral, depth, depth offset
        (0.05, 8.2, 6.6, 8.0, 0.0),
        (0.20, 8.6, 7.0, 8.8, -1.0),
        (0.35, 8.6, 6.9, 8.6, -1.6),
        (0.50, 8.0, 6.0, 7.4, -1.0),
        (0.65, 7.0, 5.2, 6.4, -0.5),
        (0.80, 6.0, 4.6, 5.6, 0.0),
        (0.95, 4.6, 4.0, 4.8, 0.2),
    ]
    for t, al, la, de, off in chain:
        F.ellipsoid(hp(t, off * u), (al * u, la * u, de * u), rot, 3.0 * u)
    # round jaw / jowl filling the throat angle
    F.ellipsoid(hp(0.42, -6.8 * u), (6.5 * u, 5.2 * u, 5.0 * u), rot, 3.5 * u)
    # masseter bulge on each cheek
    for sgn in (-1, 1):
        F.ellipsoid(hp(0.36, -2.4 * u, sgn * 5.0 * u), (5.6 * u, 2.6 * u, 5.0 * u), rot, 3.0 * u)

    # --- ears ------------------------------------------------------------------------
    for sgn in (-1, 1):
        base = hp(0.03, 1.2 * u, sgn * 3.1 * u) + np.array([0, 0, 1.0 * u])
        tip = base + np.array([-2.4 * u, sgn * 1.3 * u, 9.5 * u])
        F.capsule(base, tip, 3.0 * u, 0.9 * u, 1.8 * u)
        F.capsule(base + np.array([-2.6 * u, 0, 2.2 * u]), tip + np.array([-1.5 * u, 0, -1.2 * u]),
                  1.5 * u, 0.35 * u, 0.5 * u, mode="sub")

    # --- eyes --------------------------------------------------------------------------
    for sgn in (-1, 1):
        F.sphere(hp(0.28, 1.5 * u, sgn * 7.9 * u), 2.3 * u, 1.2 * u, mode="sub")   # socket
        F.sphere(hp(0.28, 1.5 * u, sgn * 5.9 * u), 1.6 * u, 0.8 * u)               # eyeball

    # --- nostrils --------------------------------------------------------------------
    for sgn in (-1, 1):
        F.sphere(hp(0.93, 4.3 * u, sgn * 2.2 * u), 1.25 * u, 0.6 * u, mode="sub")
    # mouth: short shallow line at the front of the lower muzzle
    F.ellipsoid(hp(0.96, -1.2 * u), (0.5 * u, 3.0 * u, 2.4 * u), rot, 0.5 * u, mode="sub")

    # --- mane: fan of flat, wavy locks rooted along the crest -----------------------
    def crest_point(z):
        i = int(np.clip(np.interp(z, neck[:, 2], np.arange(n_neck)), 0, n_neck - 1))
        a, _ = neck_radii(i / (n_neck - 1))
        return np.array([neck[i, 0] + a - 2.0 * u, 0.0, z])

    n_locks = 14
    z_top_root = zd + 42.5 * u
    z_bot_root = zd + 15.0 * u
    x_limit = 23.5 * u                      # locks end just past the base edge
    for i in range(n_locks):
        f = i / (n_locks - 1)
        root = crest_point(z_top_root + (z_bot_root - z_top_root) * f)
        root[1] = 3.4 * u * math.sin(i * 2.4)          # staggered sideways
        phi = math.radians(80.0 - 95.0 * f)             # launch angle: up (top) to slightly down (bottom)
        length = (15.0 + 7.0 * math.sin(math.pi * f) ** 0.8) * u
        tip = root + length * np.array([math.cos(phi), 0, math.sin(phi)])
        tip[0] = min(tip[0], x_limit)
        tip[2] = min(tip[2], H - 1.1 * u)
        tip[1] = root[1] * 1.5 + 1.0 * u * math.sin(i * 1.7)
        d1 = np.array([math.cos(phi + math.radians(32)), 0, math.sin(phi + math.radians(32))])
        d2 = np.array([math.cos(phi - math.radians(28)), 0, math.sin(phi - math.radians(28))])
        c1 = root + 6.5 * u * d1
        c2 = tip - 6.0 * u * d2
        pts = bezier(root, c1, c2, tip, 34)
        tt = np.linspace(0, 1, len(pts))
        # S-wave perpendicular to the lock, growing toward the tip
        tang = np.gradient(pts, axis=0)
        nrm = np.stack([-tang[:, 2], np.zeros(len(pts)), tang[:, 0]], 1)
        nrm /= np.linalg.norm(nrm, axis=1)[:, None] + 1e-9
        wave = 2.0 * u * np.sin(2 * math.pi * 1.3 * tt + i * 0.9) * tt ** 0.7
        pts = pts + nrm * wave[:, None]
        # thick flat root, tapering to a sharp (but printable) point
        radii = (2.4 * u) * (1 - tt) ** 1.3 + (0.75 * u) * (1 - (1 - tt) ** 1.3)
        F.strand(pts, radii, 1.3 * u, flat=2.0)

    # forelock between the ears, curling forward over the forehead
    root = hp(0.0, 2.2 * u, 0.6 * u) + np.array([0, 0, 2.8 * u])
    tip = hp(0.27, 7.6 * u, 3.0 * u)
    c1 = root + np.array([-4.0 * u, 0.4 * u, 3.5 * u])
    c2 = tip + np.array([0.5 * u, -1.2 * u, 5.0 * u])
    pts = bezier(root, c1, c2, tip, 24)
    tt = np.linspace(0, 1, len(pts))
    F.strand(pts, 2.3 * u * (1 - tt) ** 1.2 + 0.7 * u * (1 - (1 - tt) ** 1.2), 0.8 * u, flat=1.8)

    return F.mesh()
