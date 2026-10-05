"""Stage check: mesh + one forward solve with the initial H-frame (iron) + ferrite slab. Prints timings and field.
With Br_f=1.3 this is the old fixed-NdFeB-magnet model (old coarse run, iteration 0: 227.5 mT on the DSV surface,
214 mT volume mean).  Usage: test_forward.py [key=value ...]"""
import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config
from mriyoke.geometry import build_mesh
from mriyoke.physics import Magnetostatics
from mriyoke.levelset import Design, initial_psi_cf
from mriyoke.metrics import centre_field

cfg = Config()
for kv in sys.argv[1:]:
    k, v = kv.split("="); setattr(cfg, k, type(getattr(cfg, k))(v))
t = time.time(); mesh = build_mesh(cfg)
print(f"mesh: {mesh.ne} elements, {mesh.nv} vertices, {time.time()-t:.1f}s")
print("materials:", mesh.GetMaterials(), "boundaries:", set(mesh.GetBoundaries()))
ms = Magnetostatics(mesh, cfg)
print("ndof:", ms.fes.ndof, " free:", sum(ms.fes.FreeDofs()))
des = Design(mesh, ms, cfg)
psi, psi_f = initial_psi_cf(cfg)
des.fe.set_psi(psi); des.f.set_psi(psi_f)
des.apply(des.cr_current())
print("initial iron, ferrite volume (1/8):", des.volumes(des.cr_current()))
t = time.time(); r = ms.solve(); print(f"forward solve {time.time()-t:.1f}s  rel res {r:.1e}  converged {ms.converged}")
print(f"mean |B| in the centre sphere: {centre_field(mesh, ms, cfg)*1e3:.2f} mT")
