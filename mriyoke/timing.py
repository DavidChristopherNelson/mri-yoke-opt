"""Run timing: machine info, completion-time estimate, per-run summary rows for future estimates."""
import csv, json, os, platform, statistics, subprocess, time


def cpu_name():
    try:
        if platform.system() == "Darwin":
            return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def clock(t):
    return time.strftime("%a %H:%M", time.localtime(t))


def expected_iterations(path, ne, init, step_kg):
    """Median accepted-iteration count of earlier runs that ended by themselves with the same start,
    step cap and a similar mesh (elements within 30 %). None if there is no such run yet."""
    if not os.path.exists(path):
        return None
    its = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                if (r["stop_reason"] in ("converged", "no descent step") and r["init"] == init
                        and abs(float(r["step_kg"]) - step_kg) < 1e-9 and abs(float(r["ne"]) / ne - 1) < 0.3):
                    its.append(int(r["iterations"]))
            except (KeyError, ValueError):
                continue
    return int(statistics.median(its)) if its else None


class RunClock:
    def __init__(self, cfg, ne, ndof, threads):
        self.cfg, self.ne, self.ndof, self.threads = cfg, ne, ndof, threads
        self.t0 = time.time()
        self.step_kg = cfg.mass_step_frac_fe * cfg.iron_ref_kg
        self.exp_its = expected_iterations(cfg.run_history, ne, cfg.init, self.step_kg)
        self.dts = []

    def elapsed(self):
        return time.time() - self.t0

    def s_per_it(self):
        return statistics.median(self.dts[-15:]) if self.dts else None

    def deadline(self):
        return self.t0 + 3600 * self.cfg.time_budget_h if self.cfg.time_budget_h > 0 else None

    def out_of_time(self):
        d, s = self.deadline(), self.s_per_it()
        return d is not None and s is not None and time.time() + 1.5 * s > d

    def estimate(self, it):
        """(expected finish, latest finish) as epoch seconds; expected is None without earlier comparable runs."""
        s, now, d = self.s_per_it(), time.time(), self.deadline()
        if s is None:
            return None, d
        latest = now + (self.cfg.iter_max - it) * s
        latest = min(latest, d) if d else latest
        expected = min(now + max(self.exp_its - it, 0) * s, latest) if self.exp_its else None
        return expected, latest

    def status(self, it, m, state, n_solves, n_tries):
        expected, latest = self.estimate(it)
        return dict(state=state, it=it, iter_max=self.cfg.iter_max, started=clock(self.t0), elapsed_h=self.elapsed() / 3600,
                    s_per_it=self.s_per_it(), expected_its=self.exp_its,
                    eta_expected=clock(expected) if expected else None, eta_latest=clock(latest) if latest else None,
                    deadline=clock(self.deadline()) if self.deadline() else None,
                    mean_mT=m["blob_mean"] * 1e3, N_green=m["N_green"], F=m["F"], iron_kg=m["iron_kg"], ferrite_kg=m["ferrite_kg"], solves=n_solves, tries=n_tries,
                    updated=time.strftime("%Y-%m-%d %H:%M:%S"))

    def eta_line(self, it):
        expected, latest = self.estimate(it)
        s = self.s_per_it()
        parts = [f"{s:.0f} s/it" if s else "timing..."]
        parts.append(f"expected finish {clock(expected)} (~it {self.exp_its}, from earlier runs)" if expected
                     else "expected finish unknown (no comparable earlier run)")
        if latest:
            parts.append(f"latest finish {clock(latest)}")
        return "    eta: " + " | ".join(parts)

    def write_status(self, st):
        tmp = os.path.join(self.cfg.results_dir, "status.json.tmp")
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, os.path.join(self.cfg.results_dir, "status.json"))

    def append_history(self, it, m, stop_reason, n_solves, n_tries):
        cfg = self.cfg
        row = dict(finished=time.strftime("%Y-%m-%d %H:%M:%S"), results_dir=cfg.results_dir, host=platform.node(),
                   cpu=cpu_name(), threads=self.threads, ne=self.ne, ndof=self.ndof, fe_order=cfg.fe_order,
                   harm_order=cfg.harm_order, init=cfg.init, step_kg=self.step_kg, iterations=it, tries=n_tries,
                   solves=n_solves, total_s=round(self.elapsed()), s_per_it=round(self.s_per_it() or 0, 1),
                   stop_reason=stop_reason, mean_mT=round(m["blob_mean"] * 1e3, 3), N_green=m["N_green"], F=m["F"], cost=round(m["cost"]),
                   iron_kg=round(m["iron_kg"], 1), ferrite_kg=round(m["ferrite_kg"], 1))
        os.makedirs(os.path.dirname(cfg.run_history) or ".", exist_ok=True)
        new = not os.path.exists(cfg.run_history)
        with open(cfg.run_history, "a", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                wr.writeheader()
            wr.writerow(row)
