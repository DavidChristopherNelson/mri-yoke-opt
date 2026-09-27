"""Magnetostatics, vector potential A in HCurl (NGSolve tutorial 2.4), nonlinear iron via Newton."""
import numpy as np
from ngsolve import (HCurl, L2, GridFunction, BilinearForm, LinearForm, CoefficientFunction,
                     curl, Norm, IfPos, dx, exp, InnerProduct, Integrate, sqrt, TaskManager,
                     Preconditioner, CGSolver, Projector)
from .config import Config, NU0


def nu_iron_cf(cfg: Config, B):
    """Brauer reluctivity of |B|, clamped below nu0."""
    nB2 = InnerProduct(B, B)
    brauer = cfg.k1 * exp(cfg.k2 * nB2) + cfg.k3
    return IfPos(0.999 * NU0 - brauer, brauer, 0.999 * NU0)


class Magnetostatics:
    def __init__(self, mesh, cfg: Config):
        self.mesh, self.cfg = mesh, cfg
        self.fes = HCurl(mesh, order=cfg.fe_order, dirichlet="outer|symx|symy", nograds=True)
        self.pwc = L2(mesh, order=0)
        self.cr = GridFunction(self.pwc, name="cutratio")   # iron volume fraction per element
        self.gfA = GridFunction(self.fes, name="A")
        self.gfLam = GridFunction(self.fes, name="lambda")
        u, v = self.fes.TnT()
        self.u, self.v = u, v
        Bu = curl(u)
        self.nu_iron_u = nu_iron_cf(cfg, Bu)
        nu_design = self.cr * self.nu_iron_u + (1 - self.cr) * NU0
        self.nu_u = mesh.MaterialCF({"magnet": NU0 / cfg.mu_r_mag, "design": nu_design}, default=NU0)
        M = mesh.MaterialCF({"magnet": (0, 0, cfg.Br * NU0 / cfg.mu_r_mag)}, default=(0, 0, 0))
        self.a = BilinearForm(self.fes, symmetric=False)
        self.a += (self.nu_u * Bu) * curl(v) * dx
        self.a += cfg.reg_eps * NU0 * u * v * dx
        self.a += -M * curl(v) * dx("magnet")
        self.pre = Preconditioner(self.a, "bddc") if cfg.linear_solver == "bddc" else None
        self.B = curl(self.gfA)
        self.normB = Norm(self.B)
        self.nu_iron_A = nu_iron_cf(cfg, self.B)
        self.res = self.gfA.vec.CreateVector()
        self.proj = Projector(self.fes.FreeDofs(), True)   # zero residual on Dirichlet dofs
        self.dA = self.gfA.vec.CreateVector()
        self.vol_e = np.array(Integrate(CoefficientFunction(1), mesh, element_wise=True))
        self.design_mask = np.array(Integrate(mesh.MaterialCF({"design": 1}, default=0), mesh,
                                              element_wise=True)) > 0.5 * self.vol_e

    def _inverse(self):
        if self.cfg.linear_solver == "bddc":
            self.pre.Update()
            return CGSolver(self.a.mat, self.pre.mat, tol=1e-10, maxiter=400, printrates=False)
        return self.a.mat.Inverse(self.fes.FreeDofs(), inverse=self.cfg.linear_solver)

    def solve(self, log=print):
        """Newton with backtracking. Warm-starts from current gfA."""
        cfg = self.cfg
        free = self.fes.FreeDofs()
        with TaskManager():
            self.a.Apply(self.gfA.vec, self.res); self.res.data = self.proj * self.res
            r0 = max(abs(InnerProduct(self.res, self.res)) ** 0.5, 1e-30)
            for it in range(cfg.newton_maxit):
                self.a.AssembleLinearization(self.gfA.vec)
                inv = self._inverse()
                self.dA.data = inv * self.res
                rn_old = abs(InnerProduct(self.res, self.res)) ** 0.5
                damp = 1.0
                for _ in range(8):
                    self.gfA.vec.data -= damp * self.dA
                    self.a.Apply(self.gfA.vec, self.res); self.res.data = self.proj * self.res
                    rn = abs(InnerProduct(self.res, self.res)) ** 0.5
                    if rn < rn_old or damp < 1e-2:
                        break
                    self.gfA.vec.data += damp * self.dA
                    damp *= 0.5
                log(f"    newton {it:2d}  |r|={rn:.3e}  rel={rn / r0:.3e}  damp={damp}")
                if rn / r0 < cfg.newton_tol or rn < 1e-12:
                    break
            self.inv = inv
        return rn / r0

    def solve_adjoint(self, rhs_vec):
        """Solve K lambda = rhs with the last Newton Jacobian (symmetric)."""
        with TaskManager():
            self.a.AssembleLinearization(self.gfA.vec)
            inv = self._inverse()
            self.gfLam.vec.data = inv * rhs_vec
        return self.gfLam
