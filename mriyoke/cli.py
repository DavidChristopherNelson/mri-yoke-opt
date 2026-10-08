"""Shared entry point of scripts/run_coarse.py and run_fine.py: key=value overrides, run.log, auto-resume.

Auto-resume (auto_resume=1 on the command line, or environment MRIYOKE_AUTO_RESUME=1, as on the cloud VM): if
results_dir already holds a checkpoint (latest.json + psi/psif/mdir_latest.npy), the run continues from it in the
same directory, keeping its iteration numbering, history and viewer frames; if the run there already finished
(converged / no descent step / iter_max reached) it exits at once. A run stopped by its time budget or killed (spot
eviction) is continued. Re-running the same command is therefore idempotent."""
import json, os, sys, time
from .config import Config


def parse(cfg, args):
    flags = {}
    for kv in args:                                    # e.g. iter_max=20 maxh_design=0.04
        k, v = kv.split("=", 1)
        if k == "auto_resume":
            flags[k] = v.lower() in ("1", "true", "yes"); continue
        t = type(getattr(cfg, k))
        setattr(cfg, k, v.lower() in ("1", "true", "yes") if t is bool else t(v))
    return flags


def main(cfg: Config, args):
    flags = parse(cfg, args)
    auto = flags.get("auto_resume", os.environ.get("MRIYOKE_AUTO_RESUME", "") == "1")
    d = cfg.results_dir
    os.makedirs(d, exist_ok=True)
    note = None
    if auto and not cfg.resume and os.path.exists(os.path.join(d, "latest.json")):
        try:
            state = json.load(open(os.path.join(d, "status.json"))).get("state", "")
        except (OSError, ValueError):
            state = ""
        it_done = json.load(open(os.path.join(d, "latest.json")))["it"]
        more = "time budget" in state or ("iter_max" in state and it_done < cfg.iter_max)
        if state.startswith("finished") and not more:
            print(f"{d}: already {state}; nothing to do", flush=True)
            return
        cfg.it_offset = it_done
        cfg.resume = os.path.join(d, "psi_latest.npy")
        note = f"auto-resume from iteration {cfg.it_offset} (previous state: {state or 'killed'})"
    logf = open(os.path.join(d, "run.log"), "a")

    def log(s):
        print(s, flush=True); logf.write(s + "\n"); logf.flush()
    log(f"=== run {time.strftime('%Y-%m-%d %H:%M:%S')}  {cfg}")
    if note:
        log(note)
    from .optimize import Optimizer
    Optimizer(cfg, log).run()
