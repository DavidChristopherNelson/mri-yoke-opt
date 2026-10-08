"""Fine-mesh run (~5x the cost per solve of run_coarse.py). Same outputs, in results/fine by default.
Each step moves at most 5 % of the reference iron and ferrite masses (Config mass_step_frac_*, *_ref_kg).
Any Config field as key=value; auto_resume=1 continues an interrupted run in the same results_dir."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config
from mriyoke.cli import main

main(Config(maxh_design=0.022, maxh_dsv=0.0125, maxh_air=0.10, iter_max=250, results_dir="results/fine"), sys.argv[1:])
