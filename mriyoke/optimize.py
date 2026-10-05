"""Level-set topology optimization loop, NGSolve tutorial 7.6 pattern with adaptive penalty weight."""
import csv, json, os, time
import numpy as np
import ngsolve
from ngsolve import CoefficientFunction
from .config import Config
from .geometry import build_mesh
from .physics import Magnetostatics
from .levelset import Design, initial_psi_cf
from .sensitivity import Sensitivity
from .metrics import dsv_metrics, masses_full, costs_full, constraints_ok
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
        self.des = Design(self.mesh, self.ms, cfg)
        self.sens = Sensitivity(self.mesh, self.ms, cfg)
        self.C_ref = 8 * self.sens.V_design * cfg.iron_density * cfg.iron_cost_per_kg   # cost scale: design box full of iron
        self.frames, self.hist = [], []
        self.A_backup = self.ms.gfA.vec.CreateVector()
        self.A_best = self.ms.gfA.vec.CreateVector()
        self.A_try = self.ms.gfA.vec.CreateVector()
        self.lm, self.step_max = cfg.gn_lm, cfg.gn_step_max
        self.n_solves = self.n_tries = 0
        self.clock = RunClock(cfg, self.mesh.ne, self.ms.fes.ndof, cfg.threads or os.cpu_count())

    def evaluate(self):
        """Forward solve at current cut ratios; returns metrics dict incl. normalized misfit f, cost c and
        demagnetisation penalty."""
        cfg = self.cfg
        self.ms.solve(log=self.log if cfg.verbose_newton else (lambda s: None))
        self.n_solves += 1
        m = dsv_metrics(self.mesh, self.ms, cfg)
        v = self.des.volumes((self.ms.cr.vec.FV().NumPy(), self.ms.crf.vec.FV().NumPy()))
        m["iron_kg"], m["ferrite_kg"] = masses_full(*v, cfg)
        m["cost_fe"], m["cost_f"], m["cost"] = costs_full(*v, cfg)
        m["f"] = m["misfit"]; m["c"] = (m["cost_fe"] + m["cost_f"]) / self.C_ref
        m["rho"] = self.sens.modes()
        m["demag_P"], m["demag_frac"] = self.sens.demag()
        if not self.ms.converged:                          # reject designs the field solve could not handle
            self.log("    forward solve not converged; trial rejected")
            m["f"] = np.inf
        return m

    def objective(self, m, w):
        return w * m["f"] + m["c"] + self.cfg.demag_weight * m["demag_P"]

    def gradients(self, m, w):
        """Objective and mode sensitivities w.r.t. (iron, ferrite) fractions. Also sets the polarity of every
        element that holds no ferrite yet to the sign that lowers J, so ferrite nucleates with the best polarity
        (existing ferrite keeps its polarity: flipping it is a jump the line search could not control)."""
        cfg, ms, sens = self.cfg, self.ms, self.sens
        gm = sens.misfit_grad()
        Gm = sens.mode_grads()
        S = w * gm[2]
        gd = None
        if m["demag_P"] > 0:
            gd, d_explicit = sens.demag_grad()
            S = S + cfg.demag_weight * gd[2]
        pol, crf = ms.pol.vec.FV().NumPy(), ms.crf.vec.FV().NumPy()
        free = (crf < 1e-9) & (S != 0)
        pol[free] = -np.sign(S[free])
        c_fe, c_f = sens.cost_grad()
        g_fe = w * gm[0] + c_fe / self.C_ref
        g_f = w * sens.ferrite(gm) + c_f / self.C_ref
        if gd is not None:
            g_fe = g_fe + cfg.demag_weight * gd[0]
            g_f = g_f + cfg.demag_weight * (sens.ferrite(gd) + d_explicit)
        mask = ms.design_mask
        self.G = (Gm[:, 0, :] * mask, sens.ferrite(Gm) * mask)
        return (g_fe, g_f)

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
        Every evaluated design moves (adds + removes) at most vol_cap = (iron, ferrite) volume relative to the current design.
        Returns best (J, metrics, nu, radius) over the evaluations; ms.gfA holds that state."""
        cfg, des, ms = self.cfg, self.des, self.ms
        G = self.G
        dmodes = lambda cr: G[0] @ (cr[0] - cr_cur[0]) + G[1] @ (cr[1] - cr_cur[1])
        nu0 = np.zeros(len(G[0]))
        rho0 = m_cur["rho"] + dmodes(des.cr_trial(kappa, nu0))
        J0, best, radius = J_cur, None, self.step_max
        for _ in range(cfg.gn_max_solves):
            step = self.gn_step(des.response(kappa, nu0, G, cfg.gn_fd_eps), rho0, radius)
            if not des.within(kappa, nu0 + step, cr_cur, vol_cap):
                lo, hi = 0.0, 1.0
                for _ in range(20):
                    mid = 0.5 * (lo + hi)
                    lo, hi = (mid, hi) if des.within(kappa, nu0 + mid * step, cr_cur, vol_cap) else (lo, mid)
                step = lo * step
                radius = min(radius, float(np.abs(step).max()))   # so that halving the radius shrinks this step
            nu = nu0 + step
            des.set_trial(kappa, nu)
            m = self.evaluate()
            J = self.objective(m, w)
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
        cfg, des = self.cfg, self.des
        row = dict(it=it, mean_B=m["mean_B"], ppm=m["ppm"], iron_kg=m["iron_kg"], ferrite_kg=m["ferrite_kg"], cost=m["cost"],
                   cost_fe=m["cost_fe"], cost_f=m["cost_f"], demag_frac=m["demag_frac"], J=J, w=w, kappa=kappa,
                   f=m["f"], c=m["c"], modes=float(np.linalg.norm(m["rho"])), moved_fe_kg=m.get("moved_fe_kg", 0.0),
                   moved_f_kg=m.get("moved_f_kg", 0.0), ok=constraints_ok(m, cfg), dt=dt,
                   elapsed_s=self.clock.elapsed(), solves=self.n_solves, tries=self.n_tries)
        if it > 0:
            self.clock.dts.append(dt)
        self.hist.append(row)
        self.log(f"[it {it:3d}] meanB={m['mean_B']*1e3:8.3f} mT  ppm={m['ppm']:9.1f}  iron={m['iron_kg']:7.1f} kg  ferrite={m['ferrite_kg']:7.1f} kg  "
                 f"moved={row['moved_fe_kg']:.2f}/{row['moved_f_kg']:.2f} kg  demag={m['demag_frac']:.2f}  J={J:.4e}  w={w:.2e}  kappa={kappa:.3f}  ok={row['ok']}  ({dt:.0f}s)")
        tag = f"iter_{it:04d}"
        label = (f"iter {it}   mean B {m['mean_B']*1e3:.2f} mT   ppm {m['ppm']:.0f}   iron {m['iron_kg']:.1f} kg   ferrite {m['ferrite_kg']:.1f} kg   "
                 f"${m['cost']:.0f}   demag {m['demag_frac']:.2f}")
        export.write_vtk(self.mesh, self.ms, des, os.path.join(cfg.results_dir, tag))
        export.write_slice_png(self.mesh, self.ms, des, cfg, os.path.join(cfg.results_dir, tag + ".png"), title=label)
        self.fields = export.sample_design(self.mesh, self.ms, des, cfg)
        self.frames.append(dict(it=it, label=label, meshes=export.frame_from_fields(self.fields, cfg)))
        export.write_viewer(self.frames, cfg, os.path.join(cfg.results_dir, "viewer.html"))
        export.write_history_png(self.hist, cfg, os.path.join(cfg.results_dir, "history.png"))
        with open(os.path.join(cfg.results_dir, "history.csv"), "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(row.keys())); wr.writeheader(); wr.writerows(self.hist)
        self.log(self.clock.eta_line(it))
        self.clock.write_status(self.clock.status(it, m, "running", self.n_solves, self.n_tries))

    def save(self, tag):
        """Checkpoint: nodal iron and ferrite level sets and the element polarities."""
        d = self.cfg.results_dir
        np.save(os.path.join(d, f"psi_{tag}.npy"), self.des.fe.psi.vec.FV().NumPy())
        np.save(os.path.join(d, f"psif_{tag}.npy"), self.des.f.psi.vec.FV().NumPy())
        np.save(os.path.join(d, f"pol_{tag}.npy"), self.ms.pol.vec.FV().NumPy())

    def initial_design(self):
        cfg, des, ms = self.cfg, self.des, self.ms
        one = CoefficientFunction(1)
        psi, psi_f = initial_psi_cf(cfg) if cfg.init == "hframe" else (one, one)
        des.fe.set_psi(psi); des.f.set_psi(psi_f)
        if cfg.resume:                                         # psi_<tag>.npy of an earlier run on the same mesh (+ psif_, pol_)
            d, name = os.path.split(cfg.resume)
            des.fe.psi.vec.FV().NumPy()[:] = np.load(cfg.resume); des.fe.normalize_psi()
            des.f.psi.vec.FV().NumPy()[:] = np.load(os.path.join(d, name.replace("psi_", "psif_", 1))); des.f.normalize_psi()
            ms.pol.vec.FV().NumPy()[:] = np.load(os.path.join(d, name.replace("psi_", "pol_", 1)))
        des.apply(des.cr_current())

    def run(self):
        cfg, des, ms = self.cfg, self.des, self.ms
        self.initial_design()
        # per step and 1/8 model [m^3]: (iron, ferrite) volume that may be added + removed
        vol_cap = np.array([cfg.mass_step_frac_fe * cfg.iron_ref_kg / (8 * cfg.iron_density),
                            cfg.mass_step_frac_f * cfg.ferrite_ref_kg / (8 * cfg.ferrite_density)])
        t = time.time()
        m = self.evaluate()
        w, kappa = cfg.w_misfit0, cfg.kappa0
        J = self.objective(m, w)
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
            J = self.objective(m, w)
            # sensitivities -> update directions and mode directions (per unit volume)
            g = self.gradients(m, w)
            des.set_directions((g[0] / ms.vol_e, g[1] / ms.vol_e), (self.G[0] / ms.vol_e, self.G[1] / ms.vol_e))
            cr_cur = des.cr_current()
            self.A_backup.data = ms.gfA.vec
            # Step caps: the fixed-point part may move half the allowed iron and ferrite, the mode correction the rest.
            k_cap = des.kappa_cap(cr_cur, 0.5 * vol_cap, cfg.kappa_max)
            kappa = min(kappa, k_cap)
            if sum(des.moved(kappa, np.zeros(des.n_modes), cr_cur)) < 1e-6 * vol_cap.min():
                kappa = k_cap                                  # step would be a no-op (e.g. nothing there yet): nucleate
            # Line search: backtrack until J decreases, then keep shrinking kappa while J improves by > ls_refine_gain.
            # (Accepting the first decrease lands near the break-even step, on the far wall of the valley.)
            best = None                                        # (J, m, nu, kappa, radius)
            for _ in range(cfg.ls_max_tries):
                J_new, m_new, nu, radius = self.trial(kappa, w, m, cr_cur, J, vol_cap)
                self.n_tries += 1
                self.log(f"    try kappa={kappa:.4f}: J={J_new:.4e} (cur {J:.4e}) meanB={m_new['mean_B']*1e3:.2f} "
                         f"ppm={m_new['ppm']:.0f} iron={m_new['iron_kg']:.0f}kg ferrite={m_new['ferrite_kg']:.0f}kg |nu|={np.abs(nu).max():.2e}")
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
                des.apply(cr_cur); ms.gfA.vec.data = self.A_backup
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
            des.set_trial(kappa, nu); ms.gfA.vec.data = self.A_best
            m_new["moved_fe_kg"], m_new["moved_f_kg"] = masses_full(*des.moved(kappa, nu, cr_cur), cfg)
            kappa = min(cfg.kappa_up * kappa, cfg.kappa_max)
            des.accept()
            rel = abs(J - J_new) / max(abs(J), 1e-30)
            stall = stall + 1 if rel < cfg.dJ_rel_tol else 0
            m, J = m_new, J_new
            self.record(it, m, w, kappa, J, time.time() - t); last_it = it
            self.save("latest")
            if stall >= cfg.dJ_rel_count:
                self.log(f"[it {it}] relative change < {cfg.dJ_rel_tol} for {stall} steps; stopping")
                stop = "converged"; break
        self.save("final")
        with open(os.path.join(cfg.results_dir, "config.json"), "w") as f:
            json.dump(self.cfg.__dict__, f, indent=1)
        self.log(f"finished: {stop} after {last_it} iterations, {self.clock.elapsed() / 3600:.2f} h, {self.n_solves} field solves")
        self.clock.write_status(self.clock.status(last_it, m, "finished: " + stop, self.n_solves, self.n_tries))
        self.clock.append_history(last_it, m, stop, self.n_solves, self.n_tries)
        return self.hist
