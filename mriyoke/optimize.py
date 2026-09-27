"""Level-set topology optimization loop, NGSolve tutorial 7.6 pattern with adaptive penalty weight."""
import csv, json, os, time
import numpy as np
from .config import Config
from .geometry import build_mesh
from .physics import Magnetostatics
from .levelset import LevelSet, initial_psi_cf
from .sensitivity import Sensitivity
from .metrics import dsv_metrics, iron_mass_full, iron_cost_full, constraints_ok
from . import export


class Optimizer:
    def __init__(self, cfg: Config, log=print):
        self.cfg, self.log = cfg, log
        os.makedirs(cfg.results_dir, exist_ok=True)
        t = time.time()
        self.mesh = build_mesh(cfg)
        log(f"mesh: {self.mesh.ne} elements, {self.mesh.nv} vertices ({time.time()-t:.1f}s)")
        self.ms = Magnetostatics(self.mesh, cfg)
        log(f"HCurl order {cfg.fe_order}: {self.ms.fes.ndof} dofs")
        self.ls = LevelSet(self.mesh, self.ms, cfg)
        self.sens = Sensitivity(self.mesh, self.ms, cfg)
        self.frames, self.hist = [], []
        self.A_backup = self.ms.gfA.vec.CreateVector()

    def evaluate(self):
        """Forward solve at current cut ratio; returns metrics dict incl. normalized misfit f and cost c."""
        self.ms.solve(log=lambda s: None)
        m = dsv_metrics(self.mesh, self.ms, self.cfg)
        v = self.ls.iron_volume()
        m["f"] = m["misfit"]; m["c"] = v / self.sens.V_design
        m["iron_kg"] = iron_mass_full(v, self.cfg); m["cost"] = iron_cost_full(v, self.cfg)
        return m

    def record(self, it, m, w, kappa, J, dt):
        cfg = self.cfg
        row = dict(it=it, mean_B=m["mean_B"], ppm=m["ppm"], iron_kg=m["iron_kg"], cost=m["cost"], J=J, w=w, kappa=kappa,
                   f=m["f"], c=m["c"], ok=constraints_ok(m, cfg), dt=dt)
        self.hist.append(row)
        self.log(f"[it {it:3d}] meanB={m['mean_B']*1e3:8.3f} mT  ppm={m['ppm']:9.1f}  iron={m['iron_kg']:8.1f} kg  "
                 f"${m['cost']:8.0f}  J={J:.4e}  w={w:.2e}  kappa={kappa:.3f}  ok={row['ok']}  ({dt:.0f}s)")
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

    def run(self):
        cfg, ls, ms = self.cfg, self.ls, self.ms
        ls.set_psi(initial_psi_cf(cfg)); ls.update_cutratio()
        t = time.time()
        m = self.evaluate()
        w, kappa = cfg.w_misfit0, cfg.kappa0
        J = w * m["f"] + m["c"]
        self.record(0, m, w, kappa, J, time.time() - t)
        stall = 0
        for it in range(1, cfg.iter_max + 1):
            t = time.time()
            # adaptive penalty weight
            w = min(w * cfg.w_grow, cfg.w_max) if not constraints_ok(m, cfg) else w / cfg.w_shrink
            J = w * m["f"] + m["c"]
            # sensitivity -> update direction (per unit volume)
            g = (w * self.sens.misfit_grad() + self.sens.cost_grad()) / ms.vol_e
            ls.set_direction(g)
            self.A_backup.data = ms.gfA.vec
            accepted = False
            for _ in range(cfg.ls_max_tries):
                ls.trial_psi(kappa); ls.update_cutratio(ls.psi_new)
                m_new = self.evaluate()
                J_new = w * m_new["f"] + m_new["c"]
                self.log(f"    try kappa={kappa:.4f}: J={J_new:.4e} (cur {J:.4e}) ppm={m_new['ppm']:.0f} iron={m_new['iron_kg']:.0f}kg")
                if J_new <= J + 1e-12:
                    accepted = True; kappa = min(cfg.kappa_up * kappa, cfg.kappa_max); break
                kappa *= cfg.kappa_down
                ms.gfA.vec.data = self.A_backup
                if kappa < cfg.kappa_min:
                    break
            if not accepted:
                ls.update_cutratio(ls.psi); ms.gfA.vec.data = self.A_backup
                self.log(f"[it {it}] no descent step found (kappa={kappa:.2e}); stopping")
                break
            ls.accept()
            rel = abs(J - J_new) / max(abs(J), 1e-30)
            stall = stall + 1 if rel < cfg.dJ_rel_tol else 0
            m, J = m_new, J_new
            self.record(it, m, w, kappa, J, time.time() - t)
            if stall >= cfg.dJ_rel_count:
                self.log(f"[it {it}] relative change < {cfg.dJ_rel_tol} for {stall} steps; stopping")
                break
        with open(os.path.join(cfg.results_dir, "config.json"), "w") as f:
            json.dump(self.cfg.__dict__, f, indent=1)
        return self.hist
