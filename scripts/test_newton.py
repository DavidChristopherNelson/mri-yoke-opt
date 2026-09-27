"""Newton diagnostics: regularization / solver variants. Usage: test_newton.py <reg_eps> <solver> [linear]"""
import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config
from mriyoke.geometry import build_mesh
from mriyoke.physics import Magnetostatics
from mriyoke.levelset import LevelSet, initial_psi_cf
from mriyoke.metrics import dsv_metrics

cfg = Config(reg_eps=float(sys.argv[1]), linear_solver=sys.argv[2], maxh_design=0.045, maxh_dsv=0.03, maxh_mag=0.03)
if len(sys.argv) > 3 and sys.argv[3] == "linear":
    cfg.k1, cfg.k3 = 0.0, 570.0
mesh = build_mesh(cfg)
print(f"mesh: {mesh.ne} elements")
ms = Magnetostatics(mesh, cfg)
print("ndof:", ms.fes.ndof)
ls = LevelSet(mesh, ms, cfg); ls.set_psi(initial_psi_cf(cfg)); ls.update_cutratio()
t = time.time(); r = ms.solve(); print(f"forward solve {time.time()-t:.1f}s  final rel res {r:.2e}")
print(dsv_metrics(mesh, ms, cfg))
