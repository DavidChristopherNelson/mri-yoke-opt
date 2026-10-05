"""Level-set design representation on a fixed mesh (NGSolve tutorial 7.6 pattern).
Two level sets: psi < 0 <=> iron, psi_f < 0 <=> ferrite (ferrite wins where both claim an element).
Cut ratio = exact volume fraction of {psi<0} in each tet for linear psi."""
import numpy as np
from ngsolve import H1, GridFunction, CoefficientFunction, IfPos, x, y, z, VOL, Integrate, dx
from .config import Config


def _max(a, b):
    return IfPos(a - b, a, b)


def _box_sd(x1, y1, z0, z1, x0=None):
    """Level-set function of the box [x0, x1] x [0, y1] x [z0, z1] in the octant (negative inside): largest signed
    distance to its faces. Faces on the symmetry planes (x0 = None, y = 0, z0 = 0) are not boundaries.
    Unlike a +-1 indicator, its nodal interpolant keeps the faces where they are."""
    d = _max(_max(x - x1, y - y1), z - z1)
    if z0 > 0:
        d = _max(d, z0 - z)
    if x0 is not None:
        d = _max(d, x0 - x)
    return d


def initial_psi_cf(cfg: Config):
    """Initial guess: H-frame in 1/8 octant. Ferrite slab above the envelope, iron back plate behind it, post at
    the x=D edge, thin pole plate on the slab face (only the part outside the envelope survives).
    Returns (psi, psi_f) coefficient functions, negative inside the material."""
    D = cfg.design_L
    back = _box_sd(2 * D, cfg.slab_y / 2 + cfg.post_t, cfg.z_slab1, cfg.z_slab1 + cfg.plate_t)
    post = _box_sd(2 * D, cfg.slab_y / 2 + cfg.post_t, 0, cfg.z_slab1 + cfg.plate_t, x0=D - cfg.post_t)
    pole = _box_sd(cfg.slab_x / 2, cfg.slab_y / 2, cfg.slab_z0 - cfg.pole_t, cfg.slab_z0)
    iron = -_max(_max(-back, -post), -pole)                # union
    slab = _box_sd(cfg.slab_x / 2, cfg.slab_y / 2, cfg.slab_z0, cfg.z_slab1)
    return iron, slab


class LevelSet:
    """One level set (iron or ferrite): nodal values, update directions, exact tet cut ratio."""

    def __init__(self, mesh, ms, cfg: Config, name="psi"):
        self.mesh, self.ms, self.cfg = mesh, ms, cfg
        self.fes = H1(mesh, order=1)
        self.psi = GridFunction(self.fes, name=name)
        self.psi_new = GridFunction(self.fes)
        self.g = GridFunction(self.fes, name="g")          # normalized update direction
        self.gm = GridFunction(self.fes, name="gm")        # normalized mean-field sensitivity
        self.g_pwc = GridFunction(ms.pwc, name="sens")
        # element -> vertex index table
        self.ev = np.array([[v.nr for v in el.vertices] for el in mesh.Elements(VOL)], dtype=np.int64)
        assert self.ev.shape[1] == 4, "tets expected"
        self.vol_e = ms.vol_e
        self.design_mask = ms.design_mask

    def set_psi(self, cf):
        self.psi.Set(cf)
        self.normalize_psi()

    @staticmethod
    def cut_fraction(vals):
        """Volume fraction of {f<0} in tets, vals: (ne,4) nodal values of linear f."""
        v = np.sort(vals, axis=1).astype(np.float64)
        # break ties
        v = v + np.arange(4)[None, :] * 1e-12 * (1 + np.abs(v).max(axis=1, keepdims=True))
        frac = np.zeros(v.shape[0])
        neg = v < 0
        for i in range(4):
            others = [j for j in range(4) if j != i]
            denom = np.prod([v[:, j] - v[:, i] for j in others], axis=0)
            term = (-v[:, i]) ** 3 / denom
            frac += np.where(neg[:, i], term, 0.0)
        return np.clip(frac, 0.0, 1.0)

    def cutratio(self, nodal):
        cr = self.cut_fraction(nodal[self.ev])
        cr[~self.design_mask] = 0.0
        return cr

    def _norm(self, gf):
        return np.sqrt(Integrate(gf * gf * dx("design"), self.mesh))

    def _project(self, g_elem, gf):
        """Per-element density -> H1, normalized in L2(design) as in tutorial 7.6."""
        ge = np.array(g_elem, dtype=np.float64)
        ge[~self.design_mask] = 0.0
        self.g_pwc.vec.FV().NumPy()[:] = ge
        gf.Set(self.g_pwc)
        n = self._norm(gf)
        if n > 0:
            gf.vec.data = (1.0 / n) * gf.vec

    def normalize_psi(self):
        n = self._norm(self.psi)
        if n > 0:
            self.psi.vec.data = (1.0 / n) * self.psi.vec

    def set_directions(self, g_elem, G_modes):
        """g_elem: per-element sensitivity density of the objective (dJ/d(cut ratio) per unit volume),
        G_modes: (K+1, ne) same for the field modes (directions of the Gauss-Newton correction).
        All are multiplied by the same positive field s = (g^2 + G_0^2 + eps^2)^(-p/2) (each in rms units).
        p < 1 compresses the range but keeps the most sensitive places first, which decides where material nucleates.
        Sensitivities near the imaging volume are orders of magnitude larger than far away; the scaling keeps the
        sign of any combination g + sum mu_k G_k (the optimality condition) but lets the whole design domain move
        at a comparable rate."""
        m, vol = self.design_mask, self.vol_e
        rms = lambda a: np.sqrt(np.sum(vol[m] * a[m] ** 2) / np.sum(vol[m])) or 1.0
        ge = np.array(g_elem, dtype=np.float64); ge /= rms(ge)
        g0 = G_modes[0] / rms(G_modes[0])
        s = (ge ** 2 + g0 ** 2 + self.cfg.sens_eps ** 2) ** (-0.5 * self.cfg.sens_power)
        self._project(s * ge, self.g)
        self.g_np = self.g.vec.FV().NumPy().copy()
        self.d_np = np.zeros((len(G_modes), len(self.g_np)))
        for k, Gk in enumerate(G_modes):
            self._project(s * Gk, self.gm)
            self.d_np[k] = self.gm.vec.FV().NumPy()

    def nodal_trial(self, kappa, nu):
        return (1 - kappa) * self.psi.vec.FV().NumPy() + kappa * self.g_np + nu @ self.d_np

    def accept(self):
        self.psi.vec.data = self.psi_new.vec
        self.normalize_psi()


class Design:
    """Iron and ferrite level sets moved together: psi_new = (1-kappa) psi + kappa g + sum_k nu_k d_k for each,
    with the same kappa and nu. All cut ratios are returned as pairs (cr, crf) with cr + crf <= 1."""

    def __init__(self, mesh, ms, cfg: Config):
        self.ms, self.cfg = ms, cfg
        self.fe = LevelSet(mesh, ms, cfg, "psi")
        self.f = LevelSet(mesh, ms, cfg, "psi_f")
        self.vol_e, self.design_mask = ms.vol_e, ms.design_mask

    def _pair(self, n_fe, n_f):
        crf = self.f.cutratio(n_f)
        return np.minimum(self.fe.cutratio(n_fe), 1.0 - crf), crf      # ferrite wins

    def cr_current(self):
        return self._pair(self.fe.psi.vec.FV().NumPy(), self.f.psi.vec.FV().NumPy())

    def cr_trial(self, kappa, nu):
        return self._pair(self.fe.nodal_trial(kappa, nu), self.f.nodal_trial(kappa, nu))

    def apply(self, cr):
        """Write a cut-ratio pair into the field problem."""
        self.ms.cr.vec.FV().NumPy()[:] = cr[0]
        self.ms.crf.vec.FV().NumPy()[:] = cr[1]

    def volumes(self, cr):
        """Iron and ferrite volume of the 1/8 model."""
        return float(np.sum(cr[0] * self.vol_e)), float(np.sum(cr[1] * self.vol_e))

    def set_directions(self, g, G):
        """g = (g_fe, g_f) per-element sensitivity densities of the objective, G = (G_fe, G_f) same for the modes."""
        self.fe.set_directions(g[0], G[0])
        self.f.set_directions(g[1], G[1])
        self.n_modes = len(G[0])

    def moved(self, kappa, nu, cr_cur):
        """Iron and ferrite volume added plus removed (1/8 model) by the trial level sets, no field solve."""
        cr = self.cr_trial(kappa, nu)
        return (float(np.sum(np.abs(cr[0] - cr_cur[0]) * self.vol_e)),
                float(np.sum(np.abs(cr[1] - cr_cur[1]) * self.vol_e)))

    def within(self, kappa, nu, cr_cur, caps):
        """True if the trial moves at most caps = (iron volume, ferrite volume)."""
        mv = self.moved(kappa, nu, cr_cur)
        return mv[0] <= caps[0] and mv[1] <= caps[1]

    def kappa_cap(self, cr_cur, caps, kappa_hi):
        """Largest kappa <= kappa_hi whose fixed-point step respects both caps."""
        nu = np.zeros(self.n_modes)
        if self.within(kappa_hi, nu, cr_cur, caps):
            return kappa_hi
        lo, hi = 0.0, kappa_hi
        for _ in range(30):
            mid = 0.5 * (lo + hi)
            lo, hi = (mid, hi) if self.within(mid, nu, cr_cur, caps) else (lo, mid)
        return lo

    def response(self, kappa, nu, G, eps):
        """M[k, j] = d rho_k / d nu_j at the trial level sets, linear model: rho-sensitivities G = (G_fe, G_f)
        times the (finite-difference) cut-ratio change of a shift along mode direction j. No field solves."""
        b_fe, b_f = self.fe.nodal_trial(kappa, nu), self.f.nodal_trial(kappa, nu)
        M = np.zeros((len(G[0]), self.n_modes))
        for j in range(self.n_modes):
            p = self._pair(b_fe + eps * self.fe.d_np[j], b_f + eps * self.f.d_np[j])
            m = self._pair(b_fe - eps * self.fe.d_np[j], b_f - eps * self.f.d_np[j])
            M[:, j] = (G[0] @ (p[0] - m[0]) + G[1] @ (p[1] - m[1])) / (2 * eps)
        return M

    def set_trial(self, kappa, nu):
        """Store the trial level sets and write their cut ratios into the field problem."""
        self.fe.psi_new.vec.FV().NumPy()[:] = self.fe.nodal_trial(kappa, nu)
        self.f.psi_new.vec.FV().NumPy()[:] = self.f.nodal_trial(kappa, nu)
        self.apply(self.cr_trial(kappa, nu))

    def accept(self):
        self.fe.accept(); self.f.accept()
