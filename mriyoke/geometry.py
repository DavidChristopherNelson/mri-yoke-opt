"""1/8-octant OCC geometry: air box, design box, patient/bed envelope (keep-out and imaging region)
with the projection sphere at its centre."""
from netgen.occ import Box, Sphere, Glue, OCCGeometry, Pnt
from ngsolve import Mesh
from .config import Config


def build_geometry(cfg: Config):
    L, D = cfg.air_L, cfg.design_L
    ex, ey, ez = cfg.env_x / 2, cfg.env_y / 2, cfg.env_z / 2
    assert max(ex, ey, ez) < D, "envelope must fit in the design box"
    octant_D = Box(Pnt(0, 0, 0), Pnt(D, D, D))
    octant_L = Box(Pnt(0, 0, 0), Pnt(L, L, L))
    env_box = Box(Pnt(0, 0, 0), Pnt(ex, ey, ez))

    env = Box(Pnt(0, 0, 0), Pnt(ex, ey, ez))
    parts = []
    if cfg.proj_radius > 0:                                    # projection sphere, meshed exactly
        assert cfg.proj_radius < min(ex, ey, ez), "projection sphere must fit in the envelope"
        img = Sphere(Pnt(0, 0, 0), cfg.proj_radius) * octant_D
        img.mat("img"); img.maxh = cfg.maxh_dsv
        env = env - Sphere(Pnt(0, 0, 0), cfg.proj_radius)
        parts.append(img)
    env.mat("env"); env.maxh = cfg.maxh_dsv

    design = octant_D - env_box
    design.mat("design"); design.maxh = cfg.maxh_design

    air = octant_L - octant_D
    air.mat("air"); air.maxh = cfg.maxh_air

    shape = Glue([air, design, env] + parts)
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
