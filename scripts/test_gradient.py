"""Finite-difference check of the adjoint gradients: iron fraction, ferrite fraction (material + source term)
and the polarity-source term, for the field misfit and the demagnetisation penalty, on the H-frame start.
Usage: test_gradient.py [delta] [key=value ...]   (rel. error column should be a few % at delta = 1e-2)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from ngsolve import Integrate, dx
from mriyoke.config import Config
from mriyoke.geometry import build_mesh
from mriyoke.physics import Magnetostatics
from mriyoke.levelset import Design, initial_psi_cf
from mriyoke.sensitivity import Sensitivity

args = sys.argv[1:]
delta = float(args.pop(0)) if args and "=" not in args[0] else 1e-2
cfg = Config(Hcj_cold=400e3)                  # high enough that only part of the slab violates the gate
for kv in args:
    k, v = kv.split("="); setattr(cfg, k, type(getattr(cfg, k))(v))
mesh = build_mesh(cfg)
ms = Magnetostatics(mesh, cfg)
des = Design(mesh, ms, cfg)
sens = Sensitivity(mesh, ms, cfg)
psi, psi_f = initial_psi_cf(cfg)
des.fe.set_psi(psi); des.f.set_psi(psi_f)
des.apply(des.cr_current())
quiet = lambda s: None
ms.solve(log=quiet)
cr, crf, pol = (g.vec.FV().NumPy() for g in (ms.cr, ms.crf, ms.pol))

def misfit():
    return Integrate((ms.normB - cfg.B0) ** 2 * dx("dsv"), mesh) / (cfg.B0 ** 2 * sens.V_dsv)

P0, frac = sens.demag()
print(f"misfit {misfit():.4e}   demag penalty {P0:.4e} (violating fraction {frac:.2f})")
gm = sens.misfit_grad()
gd, d_explicit = sens.demag_grad()
# the penalty also depends on the polarity directly (h = nu_f p B_z - M_f)
nu_f, V_ref = cfg.M_f / cfg.Br_f, cfg.ferrite_ref_kg / (8 * cfg.ferrite_density)
Bz = ms.Bz_elem()
t = np.maximum(0.0, (-cfg.demag_frac * cfg.Hcj_cold - (nu_f * pol * Bz - cfg.M_f)) / cfg.Hcj_cold)
dP_dpol = crf * ms.vol_e / V_ref * 2 * t * (-nu_f * Bz / cfg.Hcj_cold)
grads = {"misfit": (misfit, gm[0], sens.ferrite(gm), crf * cfg.M_f * gm[2]),
         "demag": (lambda: sens.demag()[0], gd[0], sens.ferrite(gd) + d_explicit, crf * cfg.M_f * gd[2] + dP_dpol)}
# test elements: the most sensitive ones among iron-cut, air next to material, ferrite-cut and full-ferrite elements
A0 = ms.gfA.vec.CreateVector(); A0.data = ms.gfA.vec
print(f"{'functional':<9}{'variable':<10}{'element':>8}{'state':>7}{'adjoint':>14}{'FD':>14}{'rel.err':>9}")
for name, (fun, g_fe, g_f, g_p) in grads.items():
    cases = [("cr", cr, g_fe, (cr > 0.05) & (cr < 0.95)), ("cr", cr, g_fe, (cr < 0.01) & (crf < 0.01) & ms.design_mask),
             ("cr_f", crf, g_f, (crf > 0.05) & (crf < 0.95)), ("cr_f", crf, g_f, (crf < 0.01) & (cr < 0.01) & ms.design_mask),
             ("pol", pol, g_p, crf > 0.99), ("pol", pol, g_p, (crf > 0.05) & (crf < 0.95))]
    for var, arr, g, sel in cases:
        for e in np.flatnonzero(sel)[np.argsort(-np.abs(g[sel]))[:2]]:
            v0 = arr[e]
            lo = max(v0 - delta, 0.0) if var != "pol" else v0 - delta
            vals = []
            for v in (v0 + delta, lo):
                arr[e] = v; ms.gfA.vec.data = A0; ms.solve(log=quiet); vals.append(fun())
            arr[e] = v0
            fd = (vals[0] - vals[1]) / (v0 + delta - lo)
            print(f"{name:<9}{var:<10}{e:>8}{v0:>7.2f}{g[e]:>14.5e}{fd:>14.5e}{abs(fd - g[e]) / max(abs(fd), 1e-300):>9.1%}")
