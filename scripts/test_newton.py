"""Newton diagnostics: regularization / solver variants. Usage: test_newton.py <reg_eps> <solver> [linear]"""
import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config
from mriyoke.geometry import build_mesh
from mriyoke.physics import Magnetostatics
from mriyoke.levelset import Design, initial_psi_cf
from mriyoke.metrics import centre_field

cfg = Config(reg_eps=float(sys.argv[1]), linear_solver=sys.argv[2], maxh_design=0.045, maxh_dsv=0.03)
if len(sys.argv) > 3 and sys.argv[3] == "linear":
    cfg.k1, cfg.k3 = 0.0, 570.0
mesh = build_mesh(cfg)
print(f"mesh: {mesh.ne} elements")
ms = Magnetostatics(mesh, cfg)
print("ndof:", ms.fes.ndof)
des = Design(mesh, ms, cfg); psi, psi_f = initial_psi_cf(cfg)
des.fe.set_psi(psi); des.f.set_psi(psi_f); des.apply(des.cr_current())
t = time.time(); r = ms.solve(); print(f"forward solve {time.time()-t:.1f}s  final rel res {r:.2e}")
print(f"mean |B| in the centre sphere: {centre_field(mesh, ms, cfg)*1e3:.2f} mT")
