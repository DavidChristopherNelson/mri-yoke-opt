"""Fine-mesh run (~5x the cost per solve of run_coarse.py). Same outputs, in results/fine by default.
Each step moves at most 1 % of the reference iron and ferrite masses (Config mass_step_frac_*, *_ref_kg)."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config
from mriyoke.optimize import Optimizer

cfg = Config(maxh_design=0.022, maxh_dsv=0.0125, maxh_air=0.10,
             iter_max=250, results_dir="results/fine")
for kv in sys.argv[1:]:                       # any Config field overridable as key=value
    k, v = kv.split("="); setattr(cfg, k, type(getattr(cfg, k))(v))
os.makedirs(cfg.results_dir, exist_ok=True)
logf = open(os.path.join(cfg.results_dir, "run.log"), "a")
def log(s):
    print(s, flush=True); logf.write(s + "\n"); logf.flush()
log(f"=== run {time.strftime('%Y-%m-%d %H:%M:%S')}  {cfg}")
Optimizer(cfg, log).run()
