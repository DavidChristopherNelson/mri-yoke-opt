"""Stage 1 check: mesh + one forward solve with the initial yoke guess. Prints timings and field."""
import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config
from mriyoke.geometry import build_mesh
from mriyoke.physics import Magnetostatics
from mriyoke.levelset import LevelSet, initial_psi_cf
from mriyoke.metrics import dsv_metrics
from ngsolve import Integrate, dx

cfg = Config()
t = time.time(); mesh = build_mesh(cfg)
print(f"mesh: {mesh.ne} elements, {mesh.nv} vertices, {time.time()-t:.1f}s")
print("materials:", mesh.GetMaterials(), "boundaries:", set(mesh.GetBoundaries()))
ms = Magnetostatics(mesh, cfg)
print("ndof:", ms.fes.ndof, " free:", sum(ms.fes.FreeDofs()))
ls = LevelSet(mesh, ms, cfg)
ls.set_psi(initial_psi_cf(cfg))
ls.update_cutratio()
print("initial iron volume (1/8):", ls.iron_volume())
t = time.time(); ms.solve(); print(f"forward solve {time.time()-t:.1f}s")
print(dsv_metrics(mesh, ms, cfg))
