"""Adjoint sensitivities w.r.t. per-element iron and ferrite fractions (exact for the discretized problem).
Modes: a_k = ∫_R |B| phi_k dx / V_R [T], R the projection region, phi_k the L2(R)-orthonormal even harmonic
       polynomials (phi_0 = 1),
       so that b(x) = sum_k a_k phi_k(x) is the projection of |B| the smooth voxel count works on.
For any functional, with K λ = ∂J/∂A, K the (symmetric) Newton Jacobian:
  dJ/dcr_e   = -∫_e dnu/dcr   curl A · curl λ dx
  dJ/dcr_f_e = -∫_e dnu/dcr_f curl A · curl λ dx + M_f m_e · ∫_e curl λ dx          (material + source term)
  dJ/dm_e    =  cr_f_e M_f ∫_e curl λ dx                                            (magnetization direction)
Every gradient is returned as (iron part, ferrite material part, S_x, S_y, S_z) with S_e = ∫_e curl λ dx, so the
ferrite gradient can be re-formed for any direction without another solve."""
import numpy as np
from ngsolve import LinearForm, GridFunction, InnerProduct, IfPos, curl, dx, Integrate, CoefficientFunction, TaskManager
from .config import Config
from .imaging import EnvelopeBasis


class Sensitivity:
    def __init__(self, mesh, ms, cfg: Config):
        self.mesh, self.ms, self.cfg = mesh, ms, cfg
        self.region = "img" if cfg.proj_radius > 0 else "env"   # projection region
        self.V_env = Integrate(CoefficientFunction(1) * dx(self.region), mesh)
        self.V_design = Integrate(CoefficientFunction(1) * dx("design"), mesh)
        nB = ms.normB
        safe = IfPos(nB - 1e-12, nB, 1e-12)
        self.dnB = 1 / safe * InnerProduct(ms.B, curl(ms.v))          # derivative of |B| in direction v
        self.basis = EnvelopeBasis(cfg)
        self.phi = self.basis.cfs()
        self.lf_modes = []
        for p in self.phi:
            lf = LinearForm(ms.fes)
            lf += p / self.V_env * self.dnB * dx(self.region, bonus_intorder=cfg.harm_order)
            self.lf_modes.append(lf)

    def modes(self):
        """a_k [T] at the current state."""
        with TaskManager():
            a = [Integrate(self.ms.normB * p * dx(self.region, bonus_intorder=self.cfg.harm_order), self.mesh) for p in self.phi]
        return np.array(a) / self.V_env

    def _grad(self, lf, reassemble):
        ms = self.ms
        lf.Assemble()
        lam = ms.solve_adjoint(lf.vec, reassemble=reassemble)
        des = self.mesh.Materials("design")
        BL = InnerProduct(curl(ms.gfA), curl(lam))
        with TaskManager():
            g_fe = -np.array(Integrate(ms.dnu_dcr * BL, self.mesh, element_wise=True, definedon=des))
            g_fm = -np.array(Integrate(ms.dnu_dcrf * BL, self.mesh, element_wise=True, definedon=des))
            S = [np.array(Integrate(curl(lam)[i], self.mesh, element_wise=True, definedon=des)) for i in range(3)]
        return np.array([g_fe, g_fm] + S)

    def ferrite(self, g5):
        """Ferrite-fraction gradient(s) from the (.., 5, ne) output of the gradient routines, current directions."""
        m = self.ms.get_m()
        return g5[..., 1, :] + self.cfg.M_f * sum(m[:, i] * g5[..., 2 + i, :] for i in range(3))

    def mode_grads(self):
        """(K, 5, ne): d a_k / d cr_e, material part of d a_k / d cr_f_e, S_e (3). One adjoint solve per mode, one Jacobian."""
        return np.array([self._grad(lf, k == 0) for k, lf in enumerate(self.lf_modes)])

    def demag(self):
        """Demagnetisation gate. h_e = H·m in ferrite (pure-ferrite law, element-mean B): nu_f B·m - M_f.
        Violation t_e = max(0, (-demag_frac Hcj - h_e) / Hcj); penalty P = sum_e cr_f_e vol_e t_e^2 / V_ref,
        V_ref = reference ferrite volume (1/8 model). Returns P and the violated volume fraction of the ferrite;
        keeps what demag_grad needs."""
        cfg, ms = self.cfg, self.ms
        crf, m, vol = ms.crf.vec.FV().NumPy(), ms.get_m(), ms.vol_e
        nu_f = cfg.M_f / cfg.Br_f
        h = nu_f * np.sum(ms.B_elem() * m, axis=1) - cfg.M_f
        t = np.maximum(0.0, (-cfg.demag_frac * cfg.Hcj_cold - h) / cfg.Hcj_cold)
        V_ref = cfg.ferrite_ref_kg / (8 * cfg.ferrite_density)
        P = float(np.sum(crf * vol * t ** 2) / V_ref)
        v_f = float(np.sum(crf * vol))
        frac = float(np.sum(crf * vol * (t > 0)) / v_f) if v_f > 0 else 0.0
        q = (crf / V_ref * 2 * t * (-nu_f / cfg.Hcj_cold))[:, None] * m      # dP/dB_e / vol_e, (ne, 3)
        self._demag = (q, vol * t ** 2 / V_ref)
        return P, frac

    def demag_grad(self, reassemble=False):
        """(5, ne) adjoint pieces of the penalty of the last demag() call, and its explicit dP/dcr_f."""
        q, explicit = self._demag
        if not hasattr(self, "q_gf"):
            self.q_gf = [GridFunction(self.ms.pwc) for _ in range(3)]
            self.lf_demag = LinearForm(self.ms.fes)
            self.lf_demag += InnerProduct(CoefficientFunction(tuple(self.q_gf)), curl(self.ms.v)) * dx("design")
        for i in range(3):
            self.q_gf[i].vec.FV().NumPy()[:] = q[:, i]
        return self._grad(self.lf_demag, reassemble), explicit

    def cost_grad(self):
        """d(cost in $, full magnet)/dcr_e and /dcr_f_e."""
        cfg = self.cfg
        return (8 * self.ms.vol_e * cfg.iron_density * cfg.iron_cost_per_kg,
                8 * self.ms.vol_e * cfg.ferrite_density * cfg.ferrite_cost_per_kg)
