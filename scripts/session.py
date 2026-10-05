"""Run several design variants side by side for one session (default 10 h) with a live read-out.

  python scripts/session.py overnight "base" "clear30:clearance=0.03" "harm8:harm_order=8"
  python scripts/session.py day --hours 10 --script run_coarse.py "a:mass_step_frac=0.03" "b:sens_power=0.3"

Each variant is NAME[:key=value,key=value...] (any Config field). Results go to results/<session>/<NAME>/.
The machine's threads are split evenly between the variants; every run stops cleanly at the time budget
(checkpoint psi_latest.npy, restart with resume=...). The table below is also written to results/<session>/status.txt."""
import argparse, json, os, platform, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument("session")
ap.add_argument("variants", nargs="+")
ap.add_argument("--hours", type=float, default=10.0)
ap.add_argument("--script", default="run_fine.py")
ap.add_argument("--refresh", type=float, default=30.0)
a = ap.parse_args()

threads = max(1, (os.cpu_count() or 1) // len(a.variants))
runs = []
for v in a.variants:
    name, _, kv = v.partition(":")
    d = os.path.join("results", a.session, name)
    os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    cmd = [sys.executable, os.path.join(ROOT, "scripts", a.script), f"results_dir={d}", f"time_budget_h={a.hours}",
           f"threads={threads}"] + [x for x in kv.split(",") if x]
    if platform.system() == "Darwin":
        cmd = ["caffeinate", "-s", "-i"] + cmd                # macOS sleeps mid-run otherwise
    out = open(os.path.join(ROOT, d, "stdout.txt"), "a")
    runs.append((name, d, subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)))

t0 = time.time()
fmt = "{:<14}{:<26}{:>9}{:>8}{:>10}{:>10}{:>9}{:>8}  {:<11}{:<11}"
while True:
    lines = [f"session {a.session}: started {time.strftime('%a %H:%M', time.localtime(t0))}, "
             f"budget ends {time.strftime('%a %H:%M', time.localtime(t0 + 3600 * a.hours))}, now {time.strftime('%a %H:%M:%S')}, "
             f"{len(runs)} runs x {threads} threads",
             fmt.format("run", "state", "it", "s/it", "mean mT", "ppm", "iron kg", "hours", "expected", "latest")]
    for name, d, p in runs:
        try:
            with open(os.path.join(ROOT, d, "status.json")) as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = None
        if s is None:
            state = "starting" if p.poll() is None else f"exited ({p.returncode}), see stdout.txt"
            lines.append(fmt.format(name, state, "", "", "", "", "", "", "", ""))
            continue
        state = s["state"] if p.poll() is None or s["state"].startswith("finished") else f"crashed ({p.returncode})"
        lines.append(fmt.format(name, state, f"{s['it']}/{s['iter_max']}", f"{s['s_per_it']:.0f}" if s["s_per_it"] else "",
                                f"{s['mean_mT']:.2f}", f"{s['ppm']:.0f}", f"{s['iron_kg']:.1f}", f"{s['elapsed_h']:.2f}",
                                s["eta_expected"] or "unknown", s["eta_latest"] or ""))
    text = "\n".join(lines)
    with open(os.path.join(ROOT, "results", a.session, "status.txt"), "w") as f:
        f.write(text + "\n")
    print("\033[2J\033[H" + text, flush=True)
    if all(p.poll() is not None for _, _, p in runs):
        break
    time.sleep(a.refresh)
