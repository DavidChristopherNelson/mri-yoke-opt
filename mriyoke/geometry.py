"""1/8-octant OCC geometry: air box, design box, patient/bed envelope (keep-out) with clearance shell and DSV."""
from netgen.occ import Box, Sphere, Glue, OCCGeometry, Pnt
from ngsolve import Mesh
from .config import Config


def build_geometry(cfg: Config):
    L, D, r, rc = cfg.air_L, cfg.design_L, cfg.dsv_radius, cfg.dsv_radius + cfg.clearance
    ex, ey, ez = cfg.env_x / 2, cfg.env_y / 2, cfg.env_z / 2
    assert rc < min(ex, ey, ez) and max(ex, ey, ez) < D, "DSV + clearance must fit in the envelope, envelope in the design box"
    octant_D = Box(Pnt(0, 0, 0), Pnt(D, D, D))
    octant_L = Box(Pnt(0, 0, 0), Pnt(L, L, L))
    env_box = Box(Pnt(0, 0, 0), Pnt(ex, ey, ez))

    dsv = Sphere(Pnt(0, 0, 0), r) * octant_D
    dsv.mat("dsv"); dsv.maxh = cfg.maxh_dsv

    clear = (Sphere(Pnt(0, 0, 0), rc) - Sphere(Pnt(0, 0, 0), r)) * octant_D
    clear.mat("clear"); clear.maxh = cfg.maxh_dsv

    env = env_box - Sphere(Pnt(0, 0, 0), rc)
    env.mat("env"); env.maxh = cfg.maxh_dsv

    design = octant_D - env_box
    design.mat("design"); design.maxh = cfg.maxh_design

    air = octant_L - octant_D
    air.mat("air"); air.maxh = cfg.maxh_air

    shape = Glue([air, design, env, clear, dsv])
    tol = 1e-9
    for f in shape.faces:
        c = f.center
        if abs(c.x) < tol:
            f.name = "symx"
        elif abs(c.y) < tol:
            f.name = "symy"
        elif abs(c.z) < tol:
            f.name = "symz"
        elif abs(c.x - L) < tol or abs(c.y - L) < tol or abs(c.z - L) < tol:
            f.name = "outer"
        else:
            f.name = "inner"
    return shape


def build_mesh(cfg: Config) -> Mesh:
    geo = OCCGeometry(build_geometry(cfg))
    mesh = Mesh(geo.GenerateMesh(maxh=cfg.maxh_air))
    mesh.Curve(min(cfg.fe_order, 2))
    return mesh
