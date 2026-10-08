"""Level-set topology optimization loop (NGSolve tutorial 7.6 pattern) for Plan X:
minimise F = (C_fe + C_f + C_fixed) / N_green, the cost per good imaging voxel."""
import csv, gzip, json, os, pickle, time
import numpy as np
import ngsolve
from ngsolve import CoefficientFunction
from .config import Config
from .geometry import build_mesh
from .physics import Magnetostatics
from .levelset import Design, initial_psi_cf, noise_seed
from .sensitivity import Sensitivity
from .imaging import Imaging
from .metrics import masses_full, costs_full
from . import export
from .timing import RunClock


class Optimizer:
    def __init__(self, cfg: Config, log=print, sink=None):
        self.cfg, self.log, self.sink = cfg, log, sink
        os.makedirs(cfg.results_dir, exist_ok=True)
        if cfg.threads > 0:
            ngsolve.SetNumThreads(cfg.threads)
        t = time.time()
        mesh_pkl = os.path.join(cfg.results_dir, "mesh.pkl")
        if cfg.it_offset > 0 and os.path.exists(mesh_pkl):    # resume: the very mesh of the checkpoint (curved, pickled)
            with open(mesh_pkl, "rb") as f:
                self.mesh = pickle.load(f)
            log(f"mesh: {self.mesh.ne} elements, {self.mesh.nv} vertices, from mesh.pkl")
        else:
            self.mesh = build_mesh(cfg)
            with open(mesh_pkl, "wb") as f:
                pickle.dump(self.mesh, f)
            log(f"mesh: {self.mesh.ne} elements, {self.mesh.nv} vertices ({time.time()-t:.1f}s)")
        with open(os.path.join(cfg.results_dir, "config.json"), "w") as f:
            json.dump(cfg.__dict__, f, indent=1)
        if sink:
            sink.put_file("config.json", os.path.join(cfg.results_dir, "config.json"))
            sink.put_file("mesh.pkl", mesh_pkl)
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
        self.lm, self.step_max, self.rot = cfg.gn_lm, cfg.gn_step_max, cfg.dir_rot_max
        self.n_solves = self.n_tries = 0
        self.blob = np.zeros(self.img.shape, bool)
        self.blob_c = self.img.blob_coefs(self.blob)
        self.warned = False
        self.clock = RunClock(cfg, self.mesh.ne, self.ms.fes.ndof, cfg.threads or os.cpu_count())

    def set_widths(self, it, m):
        """Sigmoid widths of the smooth count: geometric continuation from anneal_start to anneal_end (fractions of
        dB_band and s_max), but the band width is never narrower than the present rms deviation of the projected
        field from B_c over the blob, never shrinks by more than a factor width_shrink_max per iteration, and the
        slope width is widened by the same factor. Far from the band a narrow sigmoid sees only the single closest
        voxel; the floor keeps the count a smooth function of the whole blob (with the band and slope tests
        weighted as in the sharp count) until the field is in the band; the shrink limit keeps J from turning
        stiff in one step when the mean-field correction lands in the band at once."""
        cfg = self.cfg
        r = cfg.anneal_start * (cfg.anneal_end / cfg.anneal_start) ** (min(it, cfg.anneal_iters) / max(cfg.anneal_iters, 1))
        w = max(r * cfg.dB_band, self.img.spread(m["a"], self.blob))
        if hasattr(self, "w_b"):
            w = max(w, self.w_b / cfg.width_shrink_max)
        self.w_b = w
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

    def gradients(self, m):
        """Sensitivities of J and of the blob mean field w.r.t. (iron, ferrite) fractions:
        d ln N_s / d cr = sum_k (d ln N_s / d a_k) (d a_k / d cr), one adjoint solve per mode.
        Also updates the magnetization directions: dJ/dm_e = cr_f M_f S_e, so the best direction is -S_e/|S_e|.
        Elements without ferrite take it outright (ferrite nucleates with its best direction); elements with
        ferrite turn towards it (rotate(), called by every trial with the line-search fraction) by at most
        self.rot (<= dir_rot_max), a change of the design that the line search scales together with the
        level-set step and reverts if the step fails."""
        cfg, ms, sens = self.cfg, self.ms, self.sens
        Gm = sens.mode_grads()
        _, dlogN = self.img.log_count(m["a"], self.w_b, self.w_s)
        gJ = np.tensordot(-dlogN, Gm, axes=1)                  # (5, ne) pieces of d(-ln N_s)
        S = gJ[2:].T.copy()                                    # (ne, 3)
        gd = None
        if m["demag_P"] > 0:
            gd, d_explicit = sens.demag_grad()
            S += cfg.demag_weight * gd[2:].T
        mdir, crf = ms.get_m(), ms.crf.vec.FV().NumPy()
        self.m_backup = mdir.copy()
        nS = np.linalg.norm(S, axis=1)
        ok = nS > 0
        self.m_best = np.where(ok[:, None], -S / np.where(ok, nS, 1.0)[:, None], mdir)
        free = (crf < 1e-9) & ok
        mdir[free] = self.m_best[free]                         # no ferrite there yet: no field change
        self.m_backup[free] = self.m_best[free]
        self.m_turn = ~free & ok & (crf > 0)
        ang = np.arccos(np.clip(np.sum(mdir[self.m_turn] * self.m_best[self.m_turn], axis=1), -1.0, 1.0))
        self.turned = float(np.degrees(np.mean(ang))) if ang.size else 0.0
        ms.set_m(mdir)
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

    def rotate(self, frac):
        """Turn the ferrite elements from their backed-up directions towards the best ones by frac * self.rot
        (at most the full angle)."""
        t = self.m_turn
        m0, m1 = self.m_backup[t], self.m_best[t]
        ang = np.arccos(np.clip(np.sum(m0 * m1, axis=1), -1.0, 1.0))
        f = np.minimum(1.0, frac * self.rot / np.maximum(ang, 1e-12))[:, None]
        sa = np.sin(ang)[:, None]
        a_ = ang[:, None]
        m = np.where(sa > 1e-9, (np.sin((1 - f) * a_) * m0 + np.sin(f * a_) * m1) / np.where(sa > 1e-9, sa, 1.0), m0)
        mdir = self.m_backup.copy()
        mdir[t] = m / np.linalg.norm(m, axis=1, keepdims=True)
        self.ms.set_m(mdir)

    def gn_step(self, M, rho, radius):
        """Damped least-squares shift along the mode directions that cancels the mode errors rho."""
        cfg = self.cfg
        A = M.T @ M
        dnu = -np.linalg.solve(A + (self.lm * np.trace(A) / len(A) + 1e-300) * np.eye(len(A)), M.T @ rho)
        mx = np.abs(dnu).max()
        return dnu * min(1.0, radius / mx) if mx > 0 else dnu

    def trial(self, kappa, m_cur, cr_cur, J_cur, vol_cap, k_cap):
        """Trial design psi_new = (1-kappa) psi + kappa g + sum_k nu_k d_k.
        The fixed-point part (kappa) is the tutorial 7.6 update. The mean field of the green blob is the stiff
        direction of the objective; nu is a Gauss-Newton correction that drives it to B_c: predicted from the
        linear model (no solve), then corrected with the true error after each forward solve (Newton on the
        mean), until the mean is well inside the band or gn_max_solves are used; a correction that increases the
        error halves the trust radius (cap on nu). Returned is the best J seen.
        Every evaluated design moves (adds + removes) at most vol_cap = (iron, ferrite) volume relative to the current design.
        Returns best (J, metrics, nu, radius) over the evaluations; ms.gfA holds that state."""
        cfg, des, ms = self.cfg, self.des, self.ms
        self.rotate(kappa / k_cap)                             # direction change scales with the step
        G = self.G
        dmodes = lambda cr: G[0] @ (cr[0] - cr_cur[0]) + G[1] @ (cr[1] - cr_cur[1])
        nu0 = np.zeros(len(G[0]))
        rho0 = m_cur["rho"] + dmodes(des.cr_trial(kappa, nu0))
        best, radius = None, self.step_max
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
            # Newton on the mean-field error: continue from this point with the true error, unless it grew
            if best[2] is nu or np.abs(m["rho"]).max() < np.abs(rho0).max():
                nu0, rho0 = nu, m["rho"]
            else:
                radius *= 0.5
            if np.abs(m["rho"]).max() < 0.2 * cfg.dB_band / cfg.B_c:   # mean is inside the band: corrected enough
                break
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
                   turn_deg=getattr(self, "turned", 0.0), dt=dt, elapsed_s=self.clock.elapsed(), solves=self.n_solves, tries=self.n_tries)
        if it > 0:
            self.clock.dts.append(dt)
        self.hist.append(row)
        self.log(f"[it {it:3d}] F={m['F']:.4g} $/voxel  N_green={m['N_green']}  N_smooth={N_s:.3g}  blobB={m['blob_mean']*1e3:8.3f} mT  "
                 f"iron={m['iron_kg']:7.1f} kg  ferrite={m['ferrite_kg']:7.1f} kg  moved={row['moved_fe_kg']:.2f}/{row['moved_f_kg']:.2f} kg  "
                 f"demag={m['demag_frac']:.2f}  resid={m['residual']*1e3:.3f} mT  turn={getattr(self, 'turned', 0.0):.1f}deg  J={J:.4f}  kappa={kappa:.3f}  ({dt:.0f}s)")
        tag = f"iter_{it:04d}"
        label = (f"iter {it}   F {m['F']:.4g} $/voxel   N_green {m['N_green']}   mean B {m['blob_mean']*1e3:.2f} mT   "
                 f"iron {m['iron_kg']:.1f} kg   ferrite {m['ferrite_kg']:.1f} kg   ${m['cost']:.0f}   demag {m['demag_frac']:.2f}")
        export.write_vtk(self.mesh, self.ms, des, os.path.join(cfg.results_dir, tag))
        export.write_slice_png(self.mesh, self.ms, des, cfg, os.path.join(cfg.results_dir, tag + ".png"),
                               title=label.replace("$", r"\$"))
        self.fields = export.sample_design(self.mesh, self.ms, des, cfg)
        meshes = export.frame_from_fields(self.fields, cfg)
        meshes["gr"] = export.green_surface(self.blob, cfg)
        frame = dict(it=it, label=label, meshes=meshes)
        self.frames.append(frame)
        with gzip.open(os.path.join(cfg.results_dir, "frames.jsonl.gz"), "at") as f:   # viewer frames, for resume
            f.write(json.dumps(frame) + "\n")
        export.write_viewer(self.frames, cfg, os.path.join(cfg.results_dir, "viewer.html"))
        export.write_history_png(self.hist, cfg, os.path.join(cfg.results_dir, "history.png"))
        with open(os.path.join(cfg.results_dir, "history.csv"), "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(row.keys())); wr.writeheader(); wr.writerows(self.hist)
        self.log(self.clock.eta_line(it))
        self.clock.write_status(self.clock.status(it, m, "running", self.n_solves, self.n_tries))

    def save(self, tag, it=None):
        """Checkpoint: nodal iron and ferrite level sets and the element magnetization directions (each written to a
        temporary file and renamed), then <tag>.json with the iteration number."""
        d = self.cfg.results_dir
        for name, arr in ((f"psi_{tag}", self.des.fe.psi.vec.FV().NumPy()), (f"psif_{tag}", self.des.f.psi.vec.FV().NumPy()),
                          (f"mdir_{tag}", self.ms.get_m())):
            np.save(os.path.join(d, name + ".tmp.npy"), arr)
            os.replace(os.path.join(d, name + ".tmp.npy"), os.path.join(d, name + ".npy"))
        if it is not None:
            with open(os.path.join(d, f"{tag}.json.tmp"), "w") as f:
                json.dump(dict(it=it), f)
            os.replace(os.path.join(d, f"{tag}.json.tmp"), os.path.join(d, f"{tag}.json"))

    def load_previous(self, it0):
        """Resume in the same directory: history rows and viewer frames of iterations < it0."""
        d = self.cfg.results_dir

        def val(x):
            for t in (int, float):
                try:
                    return t(x)
                except (TypeError, ValueError):
                    pass
            return x
        try:
            with open(os.path.join(d, "history.csv")) as f:
                self.hist = [{k: val(v) for k, v in r.items()} for r in csv.DictReader(f) if float(r["it"]) < it0]
            for r in self.hist:
                r["it"] = int(r["it"])
        except OSError:
            self.hist = []
        frames = {}
        try:
            with gzip.open(os.path.join(d, "frames.jsonl.gz"), "rt") as f:
                for line in f:
                    try:
                        fr = json.loads(line)
                    except ValueError:                         # line cut short by an eviction
                        continue
                    frames[fr["it"]] = fr                      # a re-recorded iteration replaces the old one
        except (OSError, EOFError):
            pass
        self.frames = [frames[k] for k in sorted(frames) if k < it0]
        with gzip.open(os.path.join(d, "frames.jsonl.gz"), "wt") as f:    # rewrite clean (an eviction may cut the tail)
            for fr in self.frames:
                f.write(json.dumps(fr) + "\n")
        self.clock.dts = [r["dt"] for r in self.hist if r["it"] > 0 and isinstance(r.get("dt"), (int, float))]

    def initial_design(self):
        cfg, des, ms = self.cfg, self.des, self.ms
        kind, _, arg = cfg.init.partition(":")
        if kind == "noise":
            coords = np.array([v.point for v in self.mesh.vertices])
            cent = coords[des.fe.ev].mean(axis=1)
            n_fe, n_f, mdir, nb = noise_seed(cfg, int(arg or 0), coords, cent)
            for ls, nodal in zip((des.fe, des.f), (n_fe, n_f)):
                ls.psi.vec.FV().NumPy()[:] = nodal; ls.normalize_psi()
            ms.set_m(mdir)
            self.log(f"noise seed {int(arg or 0)}: pitch {cfg.h_seed} m, correlation length {cfg.ell_seed} m, "
                     f"{cfg.seed_iron_frac:.0%} iron, {cfg.seed_ferrite_frac:.0%} ferrite in {nb} blobs, one random direction each")
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
            ms.set_m(np.load(os.path.join(d, name.replace("psi_", "mdir_", 1))))
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
        kappa = cfg.kappa0
        it0 = cfg.it_offset
        if it0 > 0:
            self.load_previous(it0)
            self.log(f"resumed at iteration {it0} from {cfg.resume} ({len(self.hist)} earlier history rows, {len(self.frames)} frames)")
        self.set_widths(it0, m)
        J = self.objective(m)
        self.record(it0, m, kappa, J, time.time() - t)
        self.save("latest", it0); self.persist(it0)
        stall = fails = 0
        stop, last_it = "iter_max", it0
        for it in range(it0 + 1, cfg.iter_max + 1):
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
            k_cap = max(k_cap, 1e-300)
            # Line search: backtrack until J decreases, then keep shrinking kappa while that still adds more than
            # ls_refine_gain of the decrease found so far.
            # (Accepting the first decrease lands near the break-even step, on the far wall of the valley.)
            best = None                                        # (J, m, nu, kappa, radius)
            for _ in range(cfg.ls_max_tries):
                J_new, m_new, nu, radius = self.trial(kappa, m, cr_cur, J, vol_cap, k_cap)
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
                des.apply(cr_cur); ms.gfA.vec.data = self.A_backup; ms.set_m(self.m_backup)
                fails += 1
                self.lm = min(self.lm * 10, 1e3); self.step_max = max(0.5 * min(self.step_max, self.last_radius), 1e-6)
                self.rot *= 0.5
                if fails >= cfg.ls_max_fails:
                    self.log(f"[it {it}] no descent step found (kappa={kappa:.2e}); stopping")
                    stop = "no descent step"; break
                self.log(f"[it {it}] no descent step found (kappa={kappa:.2e}); retrying with more damping (lm={self.lm:.1e})")
                kappa = k_cap
                continue
            fails = 0
            self.lm = max(self.lm / 3, cfg.gn_lm * 1e-3); self.rot = min(1.5 * self.rot, cfg.dir_rot_max)
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
            self.save("latest", it); self.persist(it)
            if stall >= cfg.dJ_rel_count:
                self.log(f"[it {it}] relative change < {cfg.dJ_rel_tol} for {stall} steps; stopping")
                stop = "converged"; break
        self.save("final", last_it)
        np.save(os.path.join(cfg.results_dir, "mask_final.npy"), export.material_mask(self.fields))   # for multistart IoU
        with open(os.path.join(cfg.results_dir, "config.json"), "w") as f:
            json.dump(self.cfg.__dict__, f, indent=1)
        self.log(f"finished: {stop} after {last_it} iterations, {self.clock.elapsed() / 3600:.2f} h, {self.n_solves} field solves")
        self.clock.write_status(self.clock.status(last_it, m, "finished: " + stop, self.n_solves, self.n_tries))
        self.clock.append_history(last_it, m, stop, self.n_solves, self.n_tries)
        self.persist_final()
        return self.hist

    def persist(self, it):
        """Queue this iteration's files for the blob store; the latest.json pointer goes last."""
        s, d = self.sink, self.cfg.results_dir
        if not s:
            return
        tag = f"iter_{it:04d}"
        s.put_file(f"{tag}.png", os.path.join(d, tag + ".png"))
        if self.cfg.blob_vtu:
            s.put_file(f"{tag}.vtu", os.path.join(d, tag + ".vtu"))
        s.put_bytes(f"frames/{it:04d}.json.gz", gzip.compress(json.dumps(self.frames[-1]).encode()))
        for rel in ("history.csv", "history.png", "status.json", "run.log"):
            s.put_file(rel, os.path.join(d, rel))
        s.put_array(f"ckpt/{it:04d}/psi.npy", self.des.fe.psi.vec.FV().NumPy())
        s.put_array(f"ckpt/{it:04d}/psif.npy", self.des.f.psi.vec.FV().NumPy())
        s.put_array(f"ckpt/{it:04d}/mdir.npy", self.ms.get_m())
        s.commit("latest.json", json.dumps(dict(it=it)).encode())

    def persist_final(self):
        s, d = self.sink, self.cfg.results_dir
        if not s:
            return
        for rel in ("psi_final.npy", "psif_final.npy", "mdir_final.npy", "mask_final.npy", "final.json", "config.json",
                    "status.json", "history.csv", "history.png", "run.log"):
            s.put_file(rel, os.path.join(d, rel))
        s.flush()
        self.log(f"blob: all uploads done ({s.failures} failed)" if s.failures else "blob: all uploads done")
