"""Magnetostatics, vector potential A in HCurl (NGSolve tutorial 2.4), nonlinear iron via Newton."""
import numpy as np
from ngsolve import (HCurl, L2, GridFunction, BilinearForm, LinearForm, CoefficientFunction,
                     curl, Norm, IfPos, dx, exp, log, InnerProduct, Integrate, sqrt, TaskManager,
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
        self.crf = GridFunction(self.pwc, name="cutratio_f")  # ferrite volume fraction per element (cr + crf <= 1)
        self.pol = GridFunction(self.pwc, name="polarity")  # ferrite magnetization direction per element, +1 or -1 (along z)
        self.pol.vec.FV().NumPy()[:] = 1.0
        self.gfA = GridFunction(self.fes, name="A")
        self.gfLam = GridFunction(self.fes, name="lambda")
        u, v = self.fes.TnT()
        self.u, self.v = u, v
        Bu = curl(u)
        self.nu_iron_u = nu_iron_cf(cfg, Bu)
        # Cut elements: geometric (log-linear) mix of the reluctivities,
        # nu = nu0^(1-cr-crf) nu_iron^cr (nu0/mu_r_f)^crf.
        # The linear mix of tutorial 7.6 makes a half-filled element air-like (mu_r ~ 2) and puts the whole
        # air->iron transition (contrast ~1400) into cr > 0.99, which wrecks Newton and the optimizer step.
        lnu_f = float(np.log(NU0 / cfg.mu_r_f))
        mix = lambda nu_iron: exp(self.cr * log(nu_iron) + self.crf * lnu_f + (1 - self.cr - self.crf) * log(NU0))
        self.nu_u = mesh.MaterialCF({"design": mix(self.nu_iron_u)}, default=NU0)
        # Ferrite is a source wherever its cut ratio is nonzero: M = crf * pol * (Br_f/mu0)/mu_r_f * e_z.
        self.a = BilinearForm(self.fes, symmetric=False)
        self.a += (self.nu_u * Bu) * curl(v) * dx
        self.a += cfg.reg_eps * NU0 * u * v * dx
        self.a += -(self.crf * self.pol * cfg.M_f) * curl(v)[2] * dx("design")
        self.pre = Preconditioner(self.a, "bddc") if cfg.linear_solver == "bddc" else None
        self.B = curl(self.gfA)
        self.normB = Norm(self.B)
        nu_iron_A = nu_iron_cf(cfg, self.B)
        self.dnu_dcr = mix(nu_iron_A) * (log(nu_iron_A) - log(NU0))
        self.dnu_dcrf = mix(nu_iron_A) * (lnu_f - log(NU0))
        self.res = self.gfA.vec.CreateVector()
        self.proj = Projector(self.fes.FreeDofs(), True)   # zero residual on Dirichlet dofs
        self.dA = self.gfA.vec.CreateVector()
        self.inv_adj = None
        self.converged = False
        self.vol_e = np.array(Integrate(CoefficientFunction(1), mesh, element_wise=True))
        self.design_mask = np.array(Integrate(mesh.MaterialCF({"design": 1}, default=0), mesh,
                                              element_wise=True)) > 0.5 * self.vol_e

    def _inverse(self):
        if self.cfg.linear_solver == "bddc":
            self.pre.Update()
            return CGSolver(self.a.mat, self.pre.mat, tol=1e-10, maxiter=400, printrates=False)
        return self.a.mat.Inverse(self.fes.FreeDofs(), inverse=self.cfg.linear_solver)

    def solve(self, log=print):
        """Newton with backtracking. Warm-starts from current gfA.
        Convergence is measured against the source residual (A = 0), not the warm-start residual: after a small
        design change the warm start is already near round-off and a relative drop can never be reached."""
        cfg = self.cfg
        with TaskManager():
            self.dA[:] = 0                                     # source residual of the current ferrite layout
            self.a.Apply(self.dA, self.res); self.res.data = self.proj * self.res
            r0 = abs(InnerProduct(self.res, self.res)) ** 0.5
            if r0 < 1e-30:                                     # no ferrite: no field
                self.gfA.vec[:] = 0
                self.converged = True
                return 0.0
            self.a.Apply(self.gfA.vec, self.res); self.res.data = self.proj * self.res
            rn = abs(InnerProduct(self.res, self.res)) ** 0.5
            if not np.isfinite(rn):                            # unusable warm start
                self.gfA.vec[:] = 0
                self.a.Apply(self.gfA.vec, self.res); self.res.data = self.proj * self.res
                rn = abs(InnerProduct(self.res, self.res)) ** 0.5
            for it in range(cfg.newton_maxit):
                if rn / r0 < cfg.newton_tol:
                    break
                self.a.AssembleLinearization(self.gfA.vec)
                inv = self._inverse()
                self.dA.data = inv * self.res
                rn_old = rn
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
                if not np.isfinite(rn):
                    break
        self.converged = bool(rn / r0 < cfg.newton_tol)
        return rn / r0

    def solve_adjoint(self, rhs_vec, reassemble=True):
        """Solve K lambda = rhs with the Newton Jacobian at the current state (symmetric).
        reassemble=False reuses the Jacobian/solver of the previous adjoint solve (same state, new rhs)."""
        with TaskManager():
            if reassemble or self.inv_adj is None:
                self.a.AssembleLinearization(self.gfA.vec)
                self.inv_adj = self._inverse()
            self.gfLam.vec.data = self.inv_adj * rhs_vec
        return self.gfLam

    def Bz_elem(self):
        """Element means of B_z (numpy, length ne)."""
        return np.array(Integrate(self.B[2], self.mesh, element_wise=True)) / self.vol_e
