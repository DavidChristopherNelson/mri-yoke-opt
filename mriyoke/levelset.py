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
        self.g_pwc = GridFunction(ms.pwc, name="sens")
        # element -> vertex index table
        self.ev = np.array([[v.nr for v in el.vertices] for el in mesh.Elements(VOL)], dtype=np.int64)
        assert self.ev.shape[1] == 4, "tets expected"
        self.vol_e = ms.vol_e
        self.design_mask = ms.design_mask

    def set_psi(self, cf):
        self.psi.Set(cf)

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

    def set_direction(self, g_elem):
        """g_elem: per-element sensitivity density (dJ/d(cut ratio) per unit volume).
        Projected to H1, normalized in L2 as in tutorial 7.6."""
        ge = np.array(g_elem, dtype=np.float64)
        ge[~self.design_mask] = 0.0
        self.g_pwc.vec.FV().NumPy()[:] = ge
        self.g.Set(self.g_pwc)
        n = np.sqrt(Integrate(self.g * self.g * dx, self.mesh))
        if n > 0:
            self.g.vec.data = (1.0 / n) * self.g.vec

    def trial_psi(self, kappa):
        self.psi_new.vec.data = (1 - kappa) * self.psi.vec + kappa * self.g.vec
        return self.psi_new

    def accept(self):
        self.psi.vec.data = self.psi_new.vec
