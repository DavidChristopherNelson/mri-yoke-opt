"""Adjoint sensitivities w.r.t. per-element iron fraction (exact for the discretized problem).
Misfit: J_f = ∫_dsv (|B|-B0)^2 dx / (B0^2 V_dsv).
Modes:  a_k = ∫_dsv (|B|/B0) phi_k dx / V_dsv, phi_k an L2(dsv)-orthonormal basis of the even harmonic polynomials
        (phi_0 = 1). rho = (a_0 - 1, a_1, ..., a_K) are the low-order field errors; |rho|^2 <= J_f.
For any functional: dJ/dcr_e = -∫_e dnu/dcr curl A · curl λ dx, with K λ = ∂J/∂A, K the (symmetric) Newton Jacobian."""
import numpy as np
from ngsolve import LinearForm, InnerProduct, IfPos, curl, dx, Integrate, CoefficientFunction, x, y, z
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
        lf.Assemble()
        lam = self.ms.solve_adjoint(lf.vec, reassemble=reassemble)
        integrand = self.ms.dnu_dcr * InnerProduct(curl(self.ms.gfA), curl(lam))
        return -np.array(Integrate(integrand, self.mesh, element_wise=True))

    def misfit_grad(self):
        """Returns per-element dJ_f/dcr_e (numpy, length ne)."""
        return self._grad(self.lf, True)

    def mode_grads(self):
        """(K+1, ne) array of d rho_k / d cr_e. Call after misfit_grad (reuses its Jacobian)."""
        return np.array([self._grad(lf, False) for lf in self.lf_modes])

    def cost_grad(self):
        """dJ_c/dcr_e for J_c = V_iron / V_design."""
        return self.ms.vol_e / self.V_design
