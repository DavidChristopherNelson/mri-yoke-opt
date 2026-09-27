"""1/8-octant OCC geometry: air box, design box, clearance shell, DSV, pole magnet."""
from netgen.occ import Box, Sphere, Glue, OCCGeometry, Pnt
from ngsolve import Mesh
from .config import Config


def build_geometry(cfg: Config):
    L, D, r, rc = cfg.air_L, cfg.design_L, cfg.dsv_radius, cfg.dsv_radius + cfg.clearance
    octant_D = Box(Pnt(0, 0, 0), Pnt(D, D, D))
    octant_L = Box(Pnt(0, 0, 0), Pnt(L, L, L))

    mag = Box(Pnt(0, 0, cfg.z_mag0), Pnt(cfg.mag_x / 2, cfg.mag_y / 2, cfg.z_mag1))
    mag.mat("magnet"); mag.maxh = cfg.maxh_mag

    dsv = Sphere(Pnt(0, 0, 0), r) * octant_D
    dsv.mat("dsv"); dsv.maxh = cfg.maxh_dsv

    clear = (Sphere(Pnt(0, 0, 0), rc) - Sphere(Pnt(0, 0, 0), r)) * octant_D
    clear.mat("clear"); clear.maxh = cfg.maxh_dsv

    design = octant_D - mag - Sphere(Pnt(0, 0, 0), rc)
    design.mat("design"); design.maxh = cfg.maxh_design

    air = octant_L - octant_D
    air.mat("air"); air.maxh = cfg.maxh_air

    shape = Glue([air, design, clear, dsv, mag])
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
