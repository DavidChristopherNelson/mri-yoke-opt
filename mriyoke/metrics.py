"""Post-hoc field metrics on the DSV surface, iron and ferrite mass and cost."""
import numpy as np
from ngsolve import Integrate, dx, CoefficientFunction
from .config import Config


def octant_sphere_points(n, r):
    """Fibonacci points on the unit sphere, kept in the x,y,z>=0 octant, scaled to radius r."""
    N = 8 * n
    i = np.arange(N) + 0.5
    phi = np.arccos(1 - 2 * i / N)
    th = np.pi * (1 + 5 ** 0.5) * i
    p = np.stack([np.sin(phi) * np.cos(th), np.sin(phi) * np.sin(th), np.cos(phi)], 1)
    p = p[(p >= 0).all(axis=1)]
    return r * p


def dsv_metrics(mesh, ms, cfg: Config, pts=None):
    if pts is None:
        pts = octant_sphere_points(cfg.n_surface_pts, cfg.dsv_radius * 0.995)
    mp = mesh(pts[:, 0], pts[:, 1], pts[:, 2])
    nB = np.asarray(ms.normB(mp)).ravel()
    Bz = np.asarray(ms.B[2](mp)).ravel()
    mean = nB.mean()
    ppm = (nB.max() - nB.min()) / mean * 1e6
    vol = Integrate(CoefficientFunction(1) * dx("dsv"), mesh)
    mean_vol = Integrate(ms.normB * dx("dsv"), mesh) / vol
    vol_mis = Integrate((ms.normB - cfg.B0) ** 2 * dx("dsv"), mesh)
    vol_var = Integrate((ms.normB - mean_vol) ** 2 * dx("dsv"), mesh)
    return dict(mean_B=mean, ppm=ppm, Bz_mean=Bz.mean(), min_B=nB.min(), max_B=nB.max(),
                misfit=vol_mis / (cfg.B0 ** 2 * vol), mean_vol=mean_vol, var=vol_var / (cfg.B0 ** 2 * vol),
                n_pts=len(pts))


def masses_full(v_fe, v_f, cfg: Config):
    """Iron and ferrite mass [kg] of the full magnet from the 1/8-model volumes."""
    return 8 * v_fe * cfg.iron_density, 8 * v_f * cfg.ferrite_density


def costs_full(v_fe, v_f, cfg: Config):
    """(C_fe, C_f, C_fe + C_f + C_fixed) in $ for the full magnet."""
    m_fe, m_f = masses_full(v_fe, v_f, cfg)
    c_fe, c_f = m_fe * cfg.iron_cost_per_kg, m_f * cfg.ferrite_cost_per_kg
    return c_fe, c_f, c_fe + c_f + cfg.C_fixed


def constraints_ok(m, cfg: Config):
    return abs(m["mean_B"] - cfg.B0) <= cfg.mean_tol and m["ppm"] <= cfg.ppm_max
