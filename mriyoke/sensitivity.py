"""Adjoint sensitivity of the field misfit w.r.t. per-element iron fraction (exact for the discretized problem).
J_f = ∫_dsv (|B|-B0)^2 dx / (B0^2 V_dsv).  dJ_f/dcr_e = -∫_e (nu_iron(|B|)-nu0) curl A · curl λ dx / (B0^2 V_dsv),
with K λ = ∂J_f/∂A, K the (symmetric) Newton Jacobian."""
import numpy as np
from ngsolve import LinearForm, InnerProduct, IfPos, curl, dx, Integrate, CoefficientFunction
from .config import Config, NU0


class Sensitivity:
    def __init__(self, mesh, ms, cfg: Config):
        self.mesh, self.ms, self.cfg = mesh, ms, cfg
        self.V_dsv = Integrate(CoefficientFunction(1) * dx("dsv"), mesh)
        self.V_design = Integrate(CoefficientFunction(1) * dx("design"), mesh)
        nB = ms.normB
        safe = IfPos(nB - 1e-12, nB, 1e-12)
        self.lf = LinearForm(ms.fes)
        self.lf += 2 * (nB - cfg.B0) / safe * InnerProduct(ms.B, curl(ms.v)) * dx("dsv")

    def misfit_grad(self):
        """Returns per-element dJ_f/dcr_e (numpy, length ne)."""
        self.lf.Assemble()
        lam = self.ms.solve_adjoint(self.lf.vec)
        integrand = (self.ms.nu_iron_A - NU0) * InnerProduct(curl(self.ms.gfA), curl(lam))
        s = np.array(Integrate(integrand, self.mesh, element_wise=True))
        return -s / (self.cfg.B0 ** 2 * self.V_dsv)

    def cost_grad(self):
        """dJ_c/dcr_e for J_c = V_iron / V_design."""
        return self.ms.vol_e / self.V_design
