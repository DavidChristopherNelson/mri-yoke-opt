"""Iron and ferrite mass and cost; mean field at the centre."""
from ngsolve import Integrate, IfPos, dx, x, y, z
from .config import Config


def centre_field(mesh, ms, cfg: Config):
    """Mean |B| over the sphere of radius blob_fallback_r at the origin [T]."""
    ball = IfPos(cfg.blob_fallback_r ** 2 - x * x - y * y - z * z, 1, 0)
    return Integrate(ball * ms.normB * dx("env|img", bonus_intorder=4), mesh) / Integrate(ball * dx("env|img", bonus_intorder=4), mesh)


def masses_full(v_fe, v_f, cfg: Config):
    """Iron and ferrite mass [kg] of the full magnet from the 1/8-model volumes."""
    return 8 * v_fe * cfg.iron_density, 8 * v_f * cfg.ferrite_density


def costs_full(v_fe, v_f, cfg: Config):
    """(C_fe, C_f, C_fe + C_f + C_fixed) in $ for the full magnet."""
    m_fe, m_f = masses_full(v_fe, v_f, cfg)
    c_fe, c_f = m_fe * cfg.iron_cost_per_kg, m_f * cfg.ferrite_cost_per_kg
    return c_fe, c_f, c_fe + c_f + cfg.C_fixed
