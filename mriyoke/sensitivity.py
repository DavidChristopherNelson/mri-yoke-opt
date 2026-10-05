"""Adjoint sensitivities w.r.t. per-element iron and ferrite fractions (exact for the discretized problem).
Misfit: J_f = ∫_dsv (|B|-B0)^2 dx / (B0^2 V_dsv).
Modes:  a_k = ∫_dsv (|B|/B0) phi_k dx / V_dsv, phi_k an L2(dsv)-orthonormal basis of the even harmonic polynomials
        (phi_0 = 1). rho = (a_0 - 1, a_1, ..., a_K) are the low-order field errors; |rho|^2 <= J_f.
For any functional, with K λ = ∂J/∂A, K the (symmetric) Newton Jacobian:
  dJ/dcr_e   = -∫_e dnu/dcr   curl A · curl λ dx
  dJ/dcr_f_e = -∫_e dnu/dcr_f curl A · curl λ dx + p_e M_f ∫_e e_z · curl λ dx      (material + source term)
  dJ/dp_e    =  cr_f_e M_f ∫_e e_z · curl λ dx                                       (polarity, p continuous)
Every gradient is returned as (iron part, ferrite material part, S) with S_e = ∫_e e_z · curl λ dx, so the
ferrite gradient can be re-formed for any polarity without another solve."""
import numpy as np
from ngsolve import LinearForm, GridFunction, InnerProduct, IfPos, curl, dx, Integrate, CoefficientFunction, TaskManager, x, y, z
from .config import Config


def even_harmonics(L):
    """Harmonic polynomials even in x, y, z up to degree L, as lists of ((i, j, k), coef) for x^2i y^2j z^2k."""
    out = []
    for h in range(L // 2 + 1):
        mono = [(i, j, h - i - j) for i in range(h + 1) for j in range(h + 1 - i)]
        if h == 0:
            out.append([(mono[0], 1.0)]); continue
        low = {m: n for n, m in enumerate((i, j, h - 1 - i - j) for i in range(h) for j in range(h - i))}
        lap = np.zeros((len(low), len(mono)))
        for c, m in enumerate(mono):
            for ax in range(3):
                if m[ax] > 0:
                    t = list(m); t[ax] -= 1
                    lap[low[tuple(t)], c] += 2 * m[ax] * (2 * m[ax] - 1)
        _, sv, vt = np.linalg.svd(lap)
        for vec in vt[len(low):]:                         # null space of the Laplacian: h + 1 polynomials
            out.append([(m, float(c)) for m, c in zip(mono, vec)])
    return out


class Sensitivity:
    def __init__(self, mesh, ms, cfg: Config):
        self.mesh, self.ms, self.cfg = mesh, ms, cfg
        self.V_dsv = Integrate(CoefficientFunction(1) * dx("dsv"), mesh)
        self.V_design = Integrate(CoefficientFunction(1) * dx("design"), mesh)
        nB = ms.normB
        safe = IfPos(nB - 1e-12, nB, 1e-12)
        self.dnB = 1 / safe * InnerProduct(ms.B, curl(ms.v))          # derivative of |B| in direction v
        self.lf = LinearForm(ms.fes)
        self.lf += 2 * (nB - cfg.B0) / (cfg.B0 ** 2 * self.V_dsv) * self.dnB * dx("dsv")
        # orthonormal even harmonic basis on the DSV (Cholesky of the Gram matrix = Gram-Schmidt, phi_0 = 1)
        R = cfg.dsv_radius
        polys = [sum(c * (x / R) ** (2 * i) * (y / R) ** (2 * j) * (z / R) ** (2 * k) for (i, j, k), c in p)
                 for p in even_harmonics(cfg.harm_order)]
        n = len(polys)
        gram = np.zeros((n, n))
        for a in range(n):
            for b in range(a + 1):
                gram[a, b] = gram[b, a] = Integrate(polys[a] * polys[b] * dx("dsv", bonus_intorder=2 * cfg.harm_order),
                                                    mesh) / self.V_dsv
        Linv = np.linalg.inv(np.linalg.cholesky(gram))
        self.phi = [sum(float(Linv[a, b]) * polys[b] for b in range(a + 1)) for a in range(n)]
        self.lf_modes = []
        for p in self.phi:
            lf = LinearForm(ms.fes)
            lf += p / (cfg.B0 * self.V_dsv) * self.dnB * dx("dsv", bonus_intorder=cfg.harm_order)
            self.lf_modes.append(lf)

    def modes(self):
        """rho = (a_0 - 1, a_1, ..., a_K) at the current state."""
        cfg = self.cfg
        a = np.array([Integrate(self.ms.normB / cfg.B0 * p * dx("dsv", bonus_intorder=cfg.harm_order), self.mesh)
                      for p in self.phi]) / self.V_dsv
        a[0] -= 1.0
        return a

    def _grad(self, lf, reassemble):
        ms = self.ms
        lf.Assemble()
        lam = ms.solve_adjoint(lf.vec, reassemble=reassemble)
        des = self.mesh.Materials("design")
        BL = InnerProduct(curl(ms.gfA), curl(lam))
        with TaskManager():
            g_fe = -np.array(Integrate(ms.dnu_dcr * BL, self.mesh, element_wise=True, definedon=des))
            g_fm = -np.array(Integrate(ms.dnu_dcrf * BL, self.mesh, element_wise=True, definedon=des))
            S = np.array(Integrate(curl(lam)[2], self.mesh, element_wise=True, definedon=des))
        return np.array([g_fe, g_fm, S])

    def ferrite(self, g3):
        """Ferrite-fraction gradient(s) from the (.., 3, ne) output of the gradient routines, current polarity."""
        return g3[..., 1, :] + self.ms.pol.vec.FV().NumPy() * self.cfg.M_f * g3[..., 2, :]

    def misfit_grad(self):
        """(3, ne): dJ_f/dcr_e, material part of dJ_f/dcr_f_e, S_e."""
        return self._grad(self.lf, True)

    def mode_grads(self):
        """(K+1, 3, ne), same for the modes rho_k. Call after misfit_grad (reuses its Jacobian)."""
        return np.array([self._grad(lf, False) for lf in self.lf_modes])

    def demag(self):
        """Demagnetisation gate. h_e = H·m in ferrite (pure-ferrite law, element-mean B): nu_f p B_z - M_f.
        Violation t_e = max(0, (-demag_frac Hcj - h_e) / Hcj); penalty P = sum_e cr_f_e vol_e t_e^2 / V_ref,
        V_ref = reference ferrite volume (1/8 model). Returns P, violated volume fraction of the ferrite and,
        if P > 0, the (3, ne) gradient pieces of P (one adjoint solve, reusing the Jacobian) plus dP/dcr_f explicit."""
        cfg, ms = self.cfg, self.ms
        crf, p, vol = ms.crf.vec.FV().NumPy(), ms.pol.vec.FV().NumPy(), ms.vol_e
        nu_f = cfg.M_f / cfg.Br_f
        h = nu_f * p * ms.Bz_elem() - cfg.M_f
        t = np.maximum(0.0, (-cfg.demag_frac * cfg.Hcj_cold - h) / cfg.Hcj_cold)
        V_ref = cfg.ferrite_ref_kg / (8 * cfg.ferrite_density)
        P = float(np.sum(crf * vol * t ** 2) / V_ref)
        v_f = float(np.sum(crf * vol))
        frac = float(np.sum(crf * vol * (t > 0)) / v_f) if v_f > 0 else 0.0
        self._demag = (crf * vol / V_ref * 2 * t * (-nu_f * p / cfg.Hcj_cold) / vol, vol * t ** 2 / V_ref)
        return P, frac

    def demag_grad(self, reassemble=False):
        """(3, ne) adjoint pieces of the penalty of the last demag() call, and its explicit dP/dcr_f."""
        q, explicit = self._demag
        if not hasattr(self, "q_gf"):
            self.q_gf = GridFunction(self.ms.pwc)
            self.lf_demag = LinearForm(self.ms.fes)
            self.lf_demag += self.q_gf * curl(self.ms.v)[2] * dx("design")
        self.q_gf.vec.FV().NumPy()[:] = q
        return self._grad(self.lf_demag, reassemble), explicit

    def cost_grad(self):
        """d(cost in $, full magnet)/dcr_e and /dcr_f_e."""
        cfg = self.cfg
        return (8 * self.ms.vol_e * cfg.iron_density * cfg.iron_cost_per_kg,
                8 * self.ms.vol_e * cfg.ferrite_density * cfg.ferrite_cost_per_kg)
