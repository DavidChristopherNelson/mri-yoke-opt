"""Level-set topology optimization loop, NGSolve tutorial 7.6 pattern with adaptive penalty weight."""
import csv, json, os, time
import numpy as np
import ngsolve
from ngsolve import CoefficientFunction
from .config import Config
from .geometry import build_mesh
from .physics import Magnetostatics
from .levelset import LevelSet, initial_psi_cf
from .sensitivity import Sensitivity
from .metrics import dsv_metrics, iron_mass_full, iron_cost_full, constraints_ok
from . import export
from .timing import RunClock


class Optimizer:
    def __init__(self, cfg: Config, log=print):
        self.cfg, self.log = cfg, log
        os.makedirs(cfg.results_dir, exist_ok=True)
        if cfg.threads > 0:
            ngsolve.SetNumThreads(cfg.threads)
        t = time.time()
        self.mesh = build_mesh(cfg)
        log(f"mesh: {self.mesh.ne} elements, {self.mesh.nv} vertices ({time.time()-t:.1f}s)")
        self.ms = Magnetostatics(self.mesh, cfg)
        log(f"HCurl order {cfg.fe_order}: {self.ms.fes.ndof} dofs")
        self.ls = LevelSet(self.mesh, self.ms, cfg)
        self.sens = Sensitivity(self.mesh, self.ms, cfg)
        self.frames, self.hist = [], []
        self.A_backup = self.ms.gfA.vec.CreateVector()
        self.A_best = self.ms.gfA.vec.CreateVector()
        self.A_try = self.ms.gfA.vec.CreateVector()
        self.lm, self.step_max = cfg.gn_lm, cfg.gn_step_max
        self.n_solves = self.n_tries = 0
        self.clock = RunClock(cfg, self.mesh.ne, self.ms.fes.ndof, cfg.threads or os.cpu_count())

    def evaluate(self):
        """Forward solve at current cut ratio; returns metrics dict incl. normalized misfit f and cost c."""
        self.ms.solve(log=self.log if self.cfg.verbose_newton else (lambda s: None))
        self.n_solves += 1
        m = dsv_metrics(self.mesh, self.ms, self.cfg)
        v = self.ls.iron_volume()
        m["f"] = m["misfit"]; m["c"] = v / self.sens.V_design
        m["rho"] = self.sens.modes()
        if not self.ms.converged:                          # reject designs the field solve could not handle
            self.log("    forward solve not converged; trial rejected")
            m["f"] = np.inf
        m["iron_kg"] = iron_mass_full(v, self.cfg); m["cost"] = iron_cost_full(v, self.cfg)
        return m

    def gn_step(self, M, rho, radius):
        """Damped least-squares shift along the mode directions that cancels the mode errors rho."""
        cfg = self.cfg
        A = M.T @ M
        dnu = -np.linalg.solve(A + (self.lm * np.trace(A) / len(A) + 1e-300) * np.eye(len(A)), M.T @ rho)
        mx = np.abs(dnu).max()
        return dnu * min(1.0, radius / mx) if mx > 0 else dnu

    def trial(self, kappa, w, m_cur, cr_cur, J_cur, vol_cap):
        """Trial design psi_new = (1-kappa) psi + kappa g + sum_k nu_k d_k.
        The fixed-point part (kappa) is the tutorial 7.6 update. The low-order field modes of the DSV (mean, z^2, ...)
        are the stiff directions of the misfit; nu is a Gauss-Newton correction for them: predicted from the
        linear model (no solve), then corrected with the true mode errors after each forward solve. A step that
        does not improve J halves the trust radius (cap on nu) and is retried from the last good point.
        Every evaluated design moves (adds + removes) at most vol_cap of iron relative to the current design.
        Returns best (J, metrics, nu, radius) over the evaluations; ms.gfA holds that state."""
        cfg, ls, ms = self.cfg, self.ls, self.ms
        nu0 = np.zeros(len(self.G))
        rho0 = m_cur["rho"] + self.G @ (ls.cr_trial(kappa, nu0) - cr_cur)
        J0, best, radius = J_cur, None, self.step_max
        for _ in range(cfg.gn_max_solves):
            step = self.gn_step(ls.response(kappa, nu0, self.G, cfg.gn_fd_eps), rho0, radius)
            if ls.moved_volume(kappa, nu0 + step, cr_cur) > vol_cap:
                lo, hi = 0.0, 1.0
                for _ in range(20):
                    mid = 0.5 * (lo + hi)
                    lo, hi = (mid, hi) if ls.moved_volume(kappa, nu0 + mid * step, cr_cur) <= vol_cap else (lo, mid)
                step = lo * step
                radius = min(radius, float(np.abs(step).max()))   # so that halving the radius shrinks this step
            nu = nu0 + step
            ls.trial_psi(kappa, nu); ls.update_cutratio(ls.psi_new)
            m = self.evaluate()
            J = w * m["f"] + m["c"]
            if best is None or J < best[0]:
                best = (J, m, nu.copy(), radius); self.A_try.data = ms.gfA.vec
            if J < J0:
                nu0, rho0, J0 = nu, m["rho"], J
            else:
                radius *= 0.5
        self.last_radius = radius
        ms.gfA.vec.data = self.A_try
        return best

    def record(self, it, m, w, kappa, J, dt):
        cfg = self.cfg
        row = dict(it=it, mean_B=m["mean_B"], ppm=m["ppm"], iron_kg=m["iron_kg"], cost=m["cost"], J=J, w=w, kappa=kappa,
                   f=m["f"], c=m["c"], modes=float(np.linalg.norm(m["rho"])), moved_kg=m.get("moved_kg", 0.0), ok=constraints_ok(m, cfg), dt=dt,
                   elapsed_s=self.clock.elapsed(), solves=self.n_solves, tries=self.n_tries)
        if it > 0:
            self.clock.dts.append(dt)
        self.hist.append(row)
        self.log(f"[it {it:3d}] meanB={m['mean_B']*1e3:8.3f} mT  ppm={m['ppm']:9.1f}  iron={m['iron_kg']:8.1f} kg  "
                 f"moved={m.get('moved_kg', 0.0):5.2f} kg  J={J:.4e}  w={w:.2e}  kappa={kappa:.3f}  ok={row['ok']}  ({dt:.0f}s)")
        tag = f"iter_{it:04d}"
        export.write_vtk(self.mesh, self.ms, self.ls, os.path.join(cfg.results_dir, tag))
        export.write_slice_png(self.mesh, self.ms, self.ls, cfg, os.path.join(cfg.results_dir, tag + ".png"),
                               title=f"iteration {it}: mean {m['mean_B']*1e3:.2f} mT, {m['ppm']:.0f} ppm, {m['iron_kg']:.0f} kg iron")
        fr = export.frame_from_psi(export.sample_psi(self.mesh, self.ls.psi, cfg), cfg)
        fr["it"] = it; fr["stats"] = {k: row[k] for k in ("mean_B", "ppm", "iron_kg", "cost", "J", "w", "kappa")}
        self.frames.append(fr)
        export.write_viewer(self.frames, cfg, os.path.join(cfg.results_dir, "viewer.html"))
        export.write_history_png(self.hist, cfg, os.path.join(cfg.results_dir, "history.png"))
        with open(os.path.join(cfg.results_dir, "history.csv"), "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(row.keys())); wr.writeheader(); wr.writerows(self.hist)
        self.log(self.clock.eta_line(it))
        self.clock.write_status(self.clock.status(it, m, "running", self.n_solves, self.n_tries))

    def run(self):
        cfg, ls, ms = self.cfg, self.ls, self.ms
        ls.set_psi(initial_psi_cf(cfg) if cfg.init == "hframe" else CoefficientFunction(1))
        if cfg.resume:                                         # nodal psi of an earlier run on the same mesh
            ls.psi.vec.FV().NumPy()[:] = np.load(cfg.resume); ls.normalize_psi()
        ls.update_cutratio()
        vol_cap = cfg.mass_step_frac * cfg.mass_step_ref_kg / (8 * cfg.iron_density)   # per step, 1/8 model [m^3]
        t = time.time()
        m = self.evaluate()
        w, kappa = cfg.w_misfit0, cfg.kappa0
        J = w * m["f"] + m["c"]
        self.record(0, m, w, kappa, J, time.time() - t)
        stall = fails = 0
        stop, last_it = "iter_max", 0
        for it in range(1, cfg.iter_max + 1):
            if self.clock.out_of_time():
                self.log(f"[it {it}] time budget of {cfg.time_budget_h} h reached; stopping (resume from psi_latest.npy)")
                stop = "time budget"; break
            t = time.time()
            # adaptive penalty weight
            w = min(w * cfg.w_grow, cfg.w_max) if not constraints_ok(m, cfg) else w / cfg.w_shrink
            J = w * m["f"] + m["c"]
            # sensitivities -> update direction and mode directions (per unit volume)
            g = (w * self.sens.misfit_grad() + self.sens.cost_grad()) / ms.vol_e
            self.G = self.sens.mode_grads() * ls.design_mask
            ls.set_directions(g, self.G / ms.vol_e)
            cr_cur = ms.cr.vec.FV().NumPy().copy()
            self.A_backup.data = ms.gfA.vec
            # Step cap: the fixed-point part may move half the allowed iron, the mode correction the rest.
            k_cap = ls.kappa_cap(cr_cur, 0.5 * vol_cap, cfg.kappa_max)
            kappa = min(kappa, k_cap)
            if ls.moved_volume(kappa, np.zeros(len(self.G)), cr_cur) < 1e-6 * vol_cap:
                kappa = k_cap                                  # step would be a no-op (e.g. no iron yet): nucleate
            # Line search: backtrack until J decreases, then keep shrinking kappa while J improves by > ls_refine_gain.
            # (Accepting the first decrease lands near the break-even step, on the far wall of the valley.)
            best = None                                        # (J, m, nu, kappa, radius)
            for _ in range(cfg.ls_max_tries):
                J_new, m_new, nu, radius = self.trial(kappa, w, m, cr_cur, J, vol_cap)
                self.n_tries += 1
                self.log(f"    try kappa={kappa:.4f}: J={J_new:.4e} (cur {J:.4e}) meanB={m_new['mean_B']*1e3:.2f} "
                         f"ppm={m_new['ppm']:.0f} iron={m_new['iron_kg']:.0f}kg |nu|={np.abs(nu).max():.2e}")
                if best is not None and J_new >= best[0]:
                    break
                small_gain = best is not None and J_new > (1 - cfg.ls_refine_gain) * best[0]
                if J_new <= J + 1e-12:
                    best = (J_new, m_new, nu, kappa, radius)
                    self.A_best.data = ms.gfA.vec
                if small_gain:
                    break
                kappa *= cfg.kappa_down
                ms.gfA.vec.data = self.A_backup
                if kappa < cfg.kappa_min:
                    break
            if best is None:
                ls.update_cutratio(ls.psi); ms.gfA.vec.data = self.A_backup
                fails += 1
                self.lm = min(self.lm * 10, 1e3); self.step_max = max(0.5 * min(self.step_max, self.last_radius), 1e-6)
                if fails >= cfg.ls_max_fails:
                    self.log(f"[it {it}] no descent step found (kappa={kappa:.2e}); stopping")
                    stop = "no descent step"; break
                self.log(f"[it {it}] no descent step found (kappa={kappa:.2e}); retrying with more damping (lm={self.lm:.1e})")
                kappa = max(kappa, 5 * cfg.kappa_min)
                continue
            fails = 0
            self.lm = max(self.lm / 3, cfg.gn_lm * 1e-3)
            J_new, m_new, nu, kappa, radius = best
            self.step_max = min(1.5 * radius, cfg.gn_step_max)
            ls.trial_psi(kappa, nu); ls.update_cutratio(ls.psi_new); ms.gfA.vec.data = self.A_best
            m_new["moved_kg"] = iron_mass_full(ls.moved_volume(kappa, nu, cr_cur), cfg)
            kappa = min(cfg.kappa_up * kappa, cfg.kappa_max)
            ls.accept()
            rel = abs(J - J_new) / max(abs(J), 1e-30)
            stall = stall + 1 if rel < cfg.dJ_rel_tol else 0
            m, J = m_new, J_new
            self.record(it, m, w, kappa, J, time.time() - t); last_it = it
            np.save(os.path.join(cfg.results_dir, "psi_latest.npy"), ls.psi.vec.FV().NumPy())
            if stall >= cfg.dJ_rel_count:
                self.log(f"[it {it}] relative change < {cfg.dJ_rel_tol} for {stall} steps; stopping")
                stop = "converged"; break
        np.save(os.path.join(cfg.results_dir, "psi_final.npy"), ls.psi.vec.FV().NumPy())
        with open(os.path.join(cfg.results_dir, "config.json"), "w") as f:
            json.dump(self.cfg.__dict__, f, indent=1)
        self.log(f"finished: {stop} after {last_it} iterations, {self.clock.elapsed() / 3600:.2f} h, {self.n_solves} field solves")
        self.clock.write_status(self.clock.status(last_it, m, "finished: " + stop, self.n_solves, self.n_tries))
        self.clock.append_history(last_it, m, stop, self.n_solves, self.n_tries)
        return self.hist
