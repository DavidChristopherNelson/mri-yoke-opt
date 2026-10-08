"""Coarse laptop run. Outputs in results/: iter_XXXX.vtu (ParaView), iter_XXXX.png (y=0 slice),
viewer.html (3D, all iterations, slider), history.png/csv, run.log.
Any Config field as key=value; auto_resume=1 continues an interrupted run in the same results_dir (see mriyoke/cli.py)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mriyoke.config import Config
from mriyoke.cli import main

main(Config(), sys.argv[1:])
