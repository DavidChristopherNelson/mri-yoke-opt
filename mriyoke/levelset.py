"""Level-set design representation on a fixed mesh (NGSolve tutorial 7.6 pattern).
psi < 0 <=> iron. Cut ratio = exact volume fraction of {psi<0} in each tet for linear psi."""
import numpy as np
from ngsolve import H1, GridFunction, CoefficientFunction, IfPos, x, y, z, VOL, Integrate, dx
from .config import Config


def _box_cf(x0, x1, y0, y1, z0, z1):
    """+1 inside box, -1 outside (as product of IfPos)."""
    inside = IfPos(x - x0, 1, 0) * IfPos(x1 - x, 1, 0) * IfPos(y - y0, 1, 0) * IfPos(y1 - y, 1, 0) \
        * IfPos(z - z0, 1, 0) * IfPos(z1 - z, 1, 0)
    return inside


def initial_psi_cf(cfg: Config):
    """Initial guess: H-frame yoke in 1/8 octant. Back plate behind magnet, post at x=D edge,
    thin pole plate on magnet face. psi = -1 inside iron, +1 outside."""
    D = cfg.design_L
    back = _box_cf(0, D, 0, cfg.mag_y / 2 + cfg.post_t, cfg.z_mag1, cfg.z_mag1 + cfg.plate_t)
    post = _box_cf(D - cfg.post_t, D, 0, cfg.mag_y / 2 + cfg.post_t, 0, cfg.z_mag1 + cfg.plate_t)
    pole = _box_cf(0, cfg.mag_x / 2, 0, cfg.mag_y / 2, cfg.z_mag0 - cfg.pole_t, cfg.z_mag0)
    iron = IfPos(back + post + pole - 0.5, 1, 0)
    return 1 - 2 * iron


class LevelSet:
    def __init__(self, mesh, ms, cfg: Config):
        self.mesh, self.ms, self.cfg = mesh, ms, cfg
        self.fes = H1(mesh, order=1)
        self.psi = GridFunction(self.fes, name="psi")
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

    def cutratio_from(self, psi_gf):
        nodal = psi_gf.vec.FV().NumPy()
        cr = self.cut_fraction(nodal[self.ev])
        cr[~self.design_mask] = 0.0
        return cr

    def update_cutratio(self, psi_gf=None):
        cr = self.cutratio_from(psi_gf or self.psi)
        self.ms.cr.vec.FV().NumPy()[:] = cr
        return cr

    def iron_volume(self, cr=None):
        if cr is None:
            cr = self.ms.cr.vec.FV().NumPy()
        return float(np.sum(cr * self.vol_e))

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
        G_modes: (K+1, ne) same for the DSV field modes (directions of the Gauss-Newton correction).
        All are multiplied by the same positive field s = (g^2 + G_0^2 + eps^2)^(-p/2) (each in rms units).
        p < 1 compresses the range but keeps the most sensitive places first, which decides where iron nucleates.
        Sensitivities near the DSV are orders of magnitude larger than far away; the scaling keeps the sign of
        any combination g + sum mu_k G_k (the optimality condition) but lets the whole design domain move at
        a comparable rate."""
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

    def cr_trial(self, kappa, nu):
        cr = self.cut_fraction(self.nodal_trial(kappa, nu)[self.ev])
        cr[~self.design_mask] = 0.0
        return cr

    def moved_volume(self, kappa, nu, cr_cur):
        """Iron volume added plus iron volume removed (1/8 model) by the trial level set, no field solve."""
        return float(np.sum(np.abs(self.cr_trial(kappa, nu) - cr_cur) * self.vol_e))

    def kappa_cap(self, cr_cur, vol_cap, kappa_hi):
        """Largest kappa <= kappa_hi whose fixed-point step moves at most vol_cap."""
        nu = np.zeros(len(self.d_np))
        if self.moved_volume(kappa_hi, nu, cr_cur) <= vol_cap:
            return kappa_hi
        lo, hi = 0.0, kappa_hi
        for _ in range(30):
            mid = 0.5 * (lo + hi)
            lo, hi = (mid, hi) if self.moved_volume(mid, nu, cr_cur) <= vol_cap else (lo, mid)
        return lo

    def response(self, kappa, nu, G_modes, eps):
        """M[k, j] = d rho_k / d nu_j at the trial level set, linear model: rho-sensitivity times the
        (finite-difference) cut-ratio change of a shift along mode direction j. No field solves."""
        base = self.nodal_trial(kappa, nu)
        M = np.zeros((len(G_modes), len(self.d_np)))
        for j, dj in enumerate(self.d_np):
            dcr = (self.cut_fraction((base + eps * dj)[self.ev]) - self.cut_fraction((base - eps * dj)[self.ev])) / (2 * eps)
            dcr[~self.design_mask] = 0.0
            M[:, j] = G_modes @ dcr
        return M

    def trial_psi(self, kappa, nu):
        self.psi_new.vec.FV().NumPy()[:] = self.nodal_trial(kappa, nu)
        return self.psi_new

    def accept(self):
        self.psi.vec.data = self.psi_new.vec
        self.normalize_psi()
