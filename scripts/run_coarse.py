"""Coarse laptop run. Outputs in results/: iter_XXXX.vtu (ParaView), iter_XXXX.png (y=0 slice),
viewer.html (3D, all iterations, slider), history.png/csv, run.log."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config
from mriyoke.optimize import Optimizer

cfg = Config()
for kv in sys.argv[1:]:                       # e.g. iter_max=20 maxh_design=0.04
    k, v = kv.split("="); setattr(cfg, k, type(getattr(cfg, k))(v))
os.makedirs(cfg.results_dir, exist_ok=True)
logf = open(os.path.join(cfg.results_dir, "run.log"), "a")
def log(s):
    print(s, flush=True); logf.write(s + "\n"); logf.flush()
log(f"=== run {time.strftime('%Y-%m-%d %H:%M:%S')}  {cfg}")
Optimizer(cfg, log).run()
