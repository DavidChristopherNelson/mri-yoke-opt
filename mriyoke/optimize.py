"""Level-set topology optimization loop (NGSolve tutorial 7.6 pattern) for Plan X:
minimise F = (C_fe + C_f + C_fixed) / N_green, the cost per good imaging voxel."""
import csv, json, os, time
import numpy as np
import ngsolve
from ngsolve import CoefficientFunction
from .config import Config
from .geometry import build_mesh
from .physics import Magnetostatics
from .levelset import Design, initial_psi_cf, noise_nodal
from .sensitivity import Sensitivity
from .imaging import Imaging
from .metrics import masses_full, costs_full
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
        self.img = Imaging(self.mesh, self.ms, cfg, self.sens.basis)
        log(f"green voxel: | |B| - {cfg.B_c*1e3:.1f} mT | <= {cfg.dB_band*5e2:.3f} mT ({cfg.band_limit} limit binds), "
            f"| d|B|/d{cfg.readout_axis} | <= {cfg.s_max*1e3:.2f} mT/m; {8*len(self.img.pts)} voxels of {cfg.dx_img*1e3:.1f} mm, "
            f"{self.sens.basis.n} modes (degree <= {cfg.harm_order})")
        self.frames, self.hist = [], []
        self.A_backup = self.ms.gfA.vec.CreateVector()
        self.A_best = self.ms.gfA.vec.CreateVector()
        self.A_try = self.ms.gfA.vec.CreateVector()
        self.lm, self.step_max = cfg.gn_lm, cfg.gn_step_max
        self.n_solves = self.n_tries = 0
        self.blob = np.zeros(self.img.shape, bool)
        self.blob_c = self.img.blob_coefs(self.blob)
        self.warned = False
        self.clock = RunClock(cfg, self.mesh.ne, self.ms.fes.ndof, cfg.threads or os.cpu_count())

    def set_widths(self, it, m):
        """Sigmoid widths of the smooth count: geometric continuation from anneal_start to anneal_end (fractions of
        dB_band and s_max), but the band width is never narrower than the present rms deviation of the projected
        field from B_c over the blob, and the slope width is widened by the same factor. Far from the band a
        narrow sigmoid sees only the single closest voxel; the floor keeps the count a smooth function of the
        whole blob (with the band and slope tests weighted as in the sharp count) until the field is in the band,
        then the schedule takes over."""
        cfg = self.cfg
        r = cfg.anneal_start * (cfg.anneal_end / cfg.anneal_start) ** (min(it, cfg.anneal_iters) / max(cfg.anneal_iters, 1))
        self.w_b = max(r * cfg.dB_band, self.img.spread(m["a"], self.blob))
        self.w_s = self.w_b * cfg.s_max / cfg.dB_band

    def evaluate(self):
        """Forward solve at current cut ratios; returns metrics dict: mode coefficients a, masses, costs,
        demagnetisation penalty and the relative mean-field error rho of the current blob."""
        cfg = self.cfg
        self.ms.solve(log=self.log if cfg.verbose_newton else (lambda s: None))
        self.n_solves += 1
        m = dict(a=self.sens.modes(), converged=self.ms.converged)
        v = self.des.volumes((self.ms.cr.vec.FV().NumPy(), self.ms.crf.vec.FV().NumPy()))
        m["iron_kg"], m["ferrite_kg"] = masses_full(*v, cfg)
        m["cost_fe"], m["cost_f"], m["cost"] = costs_full(*v, cfg)
        m["demag_P"], m["demag_frac"] = self.sens.demag()
        self.set_rho(m)
        if not m["converged"]:                              # reject designs the field solve could not handle
            self.log("    forward solve not converged; trial rejected")
        return m

    def set_rho(self, m):
        m["blob_mean"] = float(self.blob_c @ m["a"])
        m["rho"] = np.array([m["blob_mean"] / self.cfg.B_c - 1.0])

    def objective(self, m):
        """J = ln F_smooth + demagnetisation penalty, F_smooth = cost / N_smooth (full magnet) at the current widths.
        The logarithm keeps J finite while the field is far outside the band (N_smooth underflows there)."""
        logN, _ = self.img.log_count(m["a"], self.w_b, self.w_s)
        m["logN"] = logN + np.log(8.0)
        if not m["converged"]:
            return np.inf
        return float(np.log(m["cost"]) - m["logN"] + self.cfg.demag_weight * m["demag_P"])

    def count(self, m):
        """Exact green-voxel count of the current field; the largest component becomes the blob whose mean field
        the Gauss-Newton correction holds at B_c."""
        cfg, img = self.cfg, self.img
        nB = img.sample()
        self.blob, m["N_green"] = img.largest(img.green(nB))
        self.blob_c = img.blob_coefs(self.blob)
        self.set_rho(m)
        m["residual"] = img.residual(nB, m["a"], self.blob)
        m["F"] = m["cost"] / m["N_green"] if m["N_green"] else float("inf")
        if self.blob.ravel()[img.proj].any() and m["residual"] > cfg.dB_band / 4 and not self.warned:
            self.warned = True
            self.log(f"    warning: polynomial projection is off by up to {m['residual']*1e3:.3f} mT inside the green blob "
                     f"(> dB_band/4 = {cfg.dB_band*250:.3f} mT): mesh too coarse for the band; see resid= below")

    def gradients(self, m, all_polarities=False):
        """Sensitivities of J and of the blob mean field w.r.t. (iron, ferrite) fractions:
        d ln N_s / d cr = sum_k (d ln N_s / d a_k) (d a_k / d cr), one adjoint solve per mode.
        Also sets the polarity of every element that holds no ferrite yet to the sign that lowers J, so ferrite
        nucleates with the best polarity (existing ferrite keeps its polarity: flipping it is a jump the line
        search could not control). all_polarities=True sets every element (used once for noise seeds)."""
        cfg, ms, sens = self.cfg, self.ms, self.sens
        Gm = sens.mode_grads()
        _, dlogN = self.img.log_count(m["a"], self.w_b, self.w_s)
        gJ = np.tensordot(-dlogN, Gm, axes=1)                  # (3, ne) pieces of d(-ln N_s)
        S = gJ[2]
        gd = None
        if m["demag_P"] > 0:
            gd, d_explicit = sens.demag_grad()
            S = S + cfg.demag_weight * gd[2]
        pol, crf = ms.pol.vec.FV().NumPy(), ms.crf.vec.FV().NumPy()
        free = ((crf < 1e-9) | all_polarities) & (S != 0)
        pol[free] = -np.sign(S[free])
        c_fe, c_f = sens.cost_grad()
        g_fe = gJ[0] + c_fe / m["cost"]
        g_f = sens.ferrite(gJ) + c_f / m["cost"]
        if gd is not None:
            g_fe = g_fe + cfg.demag_weight * gd[0]
            g_f = g_f + cfg.demag_weight * (sens.ferrite(gd) + d_explicit)
        Gb = np.tensordot(self.blob_c / cfg.B_c, Gm, axes=1)   # blob mean field, relative
        mask = ms.design_mask
        self.G = ((Gb[0] * mask)[None], (sens.ferrite(Gb) * mask)[None])
        return (g_fe, g_f)

    def gn_step(self, M, rho, radius):
        """Damped least-squares shift along the mode directions that cancels the mode errors rho."""
        cfg = self.cfg
        A = M.T @ M
        dnu = -np.linalg.solve(A + (self.lm * np.trace(A) / len(A) + 1e-300) * np.eye(len(A)), M.T @ rho)
        mx = np.abs(dnu).max()
        return dnu * min(1.0, radius / mx) if mx > 0 else dnu

    def trial(self, kappa, m_cur, cr_cur, J_cur, vol_cap):
        """Trial design psi_new = (1-kappa) psi + kappa g + sum_k nu_k d_k.
        The fixed-point part (kappa) is the tutorial 7.6 update. The mean field of the green blob is the stiff
        direction of the objective; nu is a Gauss-Newton correction that drives it to B_c: predicted from the
        linear model (no solve), then corrected with the true error after each forward solve. A step that
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
                if best is not None and lo < 0.05:             # mass-step budget used up: nothing left to correct with
                    break
                step = lo * step
                radius = min(radius, float(np.abs(step).max()))   # so that halving the radius shrinks this step
            nu = nu0 + step
            des.set_trial(kappa, nu)
            m = self.evaluate()
            J = self.objective(m)
            if best is None or J < best[0]:
                best = (J, m, nu.copy(), radius); self.A_try.data = ms.gfA.vec
            if J < J0:
                nu0, rho0, J0 = nu, m["rho"], J
            else:
                radius *= 0.5
        self.last_radius = radius
        ms.gfA.vec.data = self.A_try
        return best

    def record(self, it, m, kappa, J, dt):
        cfg, des = self.cfg, self.des
        N_s = float(np.exp(m["logN"]))
        row = dict(it=it, F=m["F"], F_smooth=m["cost"] / N_s if N_s > 0 else float("inf"), N_green=m["N_green"], N_smooth=N_s,
                   blob_mean=m["blob_mean"], cost=m["cost"], cost_fe=m["cost_fe"], cost_f=m["cost_f"], iron_kg=m["iron_kg"],
                   ferrite_kg=m["ferrite_kg"], demag_frac=m["demag_frac"], residual=m["residual"], J=J, kappa=kappa,
                   width=self.w_b / cfg.dB_band, moved_fe_kg=m.get("moved_fe_kg", 0.0), moved_f_kg=m.get("moved_f_kg", 0.0),
                   dt=dt, elapsed_s=self.clock.elapsed(), solves=self.n_solves, tries=self.n_tries)
        if it > 0:
            self.clock.dts.append(dt)
        self.hist.append(row)
        self.log(f"[it {it:3d}] F={m['F']:.4g} $/voxel  N_green={m['N_green']}  N_smooth={N_s:.3g}  blobB={m['blob_mean']*1e3:8.3f} mT  "
                 f"iron={m['iron_kg']:7.1f} kg  ferrite={m['ferrite_kg']:7.1f} kg  moved={row['moved_fe_kg']:.2f}/{row['moved_f_kg']:.2f} kg  "
                 f"demag={m['demag_frac']:.2f}  resid={m['residual']*1e3:.3f} mT  J={J:.4f}  kappa={kappa:.3f}  ({dt:.0f}s)")
        tag = f"iter_{it:04d}"
        label = (f"iter {it}   F {m['F']:.4g} $/voxel   N_green {m['N_green']}   mean B {m['blob_mean']*1e3:.2f} mT   "
                 f"iron {m['iron_kg']:.1f} kg   ferrite {m['ferrite_kg']:.1f} kg   ${m['cost']:.0f}   demag {m['demag_frac']:.2f}")
        export.write_vtk(self.mesh, self.ms, des, os.path.join(cfg.results_dir, tag))
        export.write_slice_png(self.mesh, self.ms, des, cfg, os.path.join(cfg.results_dir, tag + ".png"),
                               title=label.replace("$", r"\$"))
        self.fields = export.sample_design(self.mesh, self.ms, des, cfg)
        meshes = export.frame_from_fields(self.fields, cfg)
        meshes["gr"] = export.green_surface(self.blob, cfg)
        self.frames.append(dict(it=it, label=label, meshes=meshes))
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
        kind, _, arg = cfg.init.partition(":")
        if kind == "noise":
            coords = np.array([v.point for v in self.mesh.vertices])
            for ls, nodal in zip((des.fe, des.f), noise_nodal(cfg, int(arg or 0), coords)):
                ls.psi.vec.FV().NumPy()[:] = nodal; ls.normalize_psi()
            self.log(f"noise seed {int(arg or 0)}: pitch {cfg.h_seed} m, correlation length {cfg.ell_seed} m, "
                     f"{cfg.seed_iron_frac:.0%} iron, {cfg.seed_ferrite_frac:.0%} ferrite")
        elif kind == "hframe":
            if arg:
                cfg.slab_t = {"thin": 0.030, "medium": 0.050, "thick": 0.080}[arg]
            psi, psi_f = initial_psi_cf(cfg)
            des.fe.set_psi(psi); des.f.set_psi(psi_f)
        else:
            des.fe.set_psi(CoefficientFunction(1)); des.f.set_psi(CoefficientFunction(1))
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
        self.count(m)
        if cfg.init.startswith("noise") and not cfg.resume:    # polarity of every element from one forward + adjoint solve
            self.set_widths(0, m)
            self.gradients(m, all_polarities=True)
            pol = ms.pol.vec.FV().NumPy()
            self.log(f"polarities set from the adjoint: {np.mean(pol[ms.design_mask] > 0):.0%} of the design elements +z")
            m = self.evaluate()
            self.count(m)
        kappa = cfg.kappa0
        self.set_widths(0, m)
        J = self.objective(m)
        self.record(0, m, kappa, J, time.time() - t)
        stall = fails = 0
        stop, last_it = "iter_max", 0
        for it in range(1, cfg.iter_max + 1):
            if self.clock.out_of_time():
                self.log(f"[it {it}] time budget of {cfg.time_budget_h} h reached; stopping (resume from psi_latest.npy)")
                stop = "time budget"; break
            t = time.time()
            self.set_widths(it, m)
            J = self.objective(m)                              # same design, this iteration's widths
            # sensitivities -> update directions and mean-field direction (per unit volume)
            g = self.gradients(m)
            des.set_directions((g[0] / ms.vol_e, g[1] / ms.vol_e), (self.G[0] / ms.vol_e, self.G[1] / ms.vol_e))
            cr_cur = des.cr_current()
            self.A_backup.data = ms.gfA.vec
            # Step caps: the fixed-point part may move half the allowed iron and ferrite, the mean-field correction the rest.
            k_cap = des.kappa_cap(cr_cur, 0.5 * vol_cap, cfg.kappa_max)
            kappa = min(kappa, k_cap)
            if sum(des.moved(kappa, np.zeros(des.n_modes), cr_cur)) < 1e-6 * vol_cap.min():
                kappa = k_cap                                  # step would be a no-op (e.g. nothing there yet): nucleate
            # Line search: backtrack until J decreases, then keep shrinking kappa while that still adds more than
            # ls_refine_gain of the decrease found so far.
            # (Accepting the first decrease lands near the break-even step, on the far wall of the valley.)
            best = None                                        # (J, m, nu, kappa, radius)
            for _ in range(cfg.ls_max_tries):
                J_new, m_new, nu, radius = self.trial(kappa, m, cr_cur, J, vol_cap)
                self.n_tries += 1
                self.log(f"    try kappa={kappa:.4f}: J={J_new:.4f} (cur {J:.4f}) blobB={m_new['blob_mean']*1e3:.2f} "
                         f"iron={m_new['iron_kg']:.0f}kg ferrite={m_new['ferrite_kg']:.0f}kg |nu|={np.abs(nu).max():.2e}")
                if best is not None and J_new >= best[0]:
                    break
                small_gain = best is not None and best[0] - J_new < cfg.ls_refine_gain * (J - best[0])
                if J_new <= J + 1e-12:
                    best = (J_new, m_new, nu, kappa, radius)
                    self.A_best.data = ms.gfA.vec
                if small_gain:
                    break
                kappa *= cfg.kappa_down
                ms.gfA.vec.data = self.A_backup
                if kappa < cfg.kappa_min * k_cap:              # relative to the capped step
                    break
            if best is None:
                des.apply(cr_cur); ms.gfA.vec.data = self.A_backup
                fails += 1
                self.lm = min(self.lm * 10, 1e3); self.step_max = max(0.5 * min(self.step_max, self.last_radius), 1e-6)
                if fails >= cfg.ls_max_fails:
                    self.log(f"[it {it}] no descent step found (kappa={kappa:.2e}); stopping")
                    stop = "no descent step"; break
                self.log(f"[it {it}] no descent step found (kappa={kappa:.2e}); retrying with more damping (lm={self.lm:.1e})")
                kappa = k_cap
                continue
            fails = 0
            self.lm = max(self.lm / 3, cfg.gn_lm * 1e-3)
            J_new, m_new, nu, kappa, radius = best
            self.step_max = min(1.5 * radius, cfg.gn_step_max)
            des.set_trial(kappa, nu); ms.gfA.vec.data = self.A_best
            m_new["moved_fe_kg"], m_new["moved_f_kg"] = masses_full(*des.moved(kappa, nu, cr_cur), cfg)
            kappa = min(cfg.kappa_up * kappa, cfg.kappa_max)
            des.accept()
            # J = ln F: its change is the relative change of F. Not a stopping signal while the widths still shrink.
            stall = stall + 1 if abs(J - J_new) < cfg.dJ_rel_tol and it >= cfg.anneal_iters else 0
            m, J = m_new, J_new
            self.count(m)
            self.record(it, m, kappa, J, time.time() - t); last_it = it
            self.save("latest")
            if stall >= cfg.dJ_rel_count:
                self.log(f"[it {it}] relative change < {cfg.dJ_rel_tol} for {stall} steps; stopping")
                stop = "converged"; break
        self.save("final")
        np.save(os.path.join(cfg.results_dir, "mask_final.npy"), export.material_mask(self.fields))   # for multistart IoU
        with open(os.path.join(cfg.results_dir, "config.json"), "w") as f:
            json.dump(self.cfg.__dict__, f, indent=1)
        self.log(f"finished: {stop} after {last_it} iterations, {self.clock.elapsed() / 3600:.2f} h, {self.n_solves} field solves")
        self.clock.write_status(self.clock.status(last_it, m, "finished: " + stop, self.n_solves, self.n_tries))
        self.clock.append_history(last_it, m, stop, self.n_solves, self.n_tries)
        return self.hist
