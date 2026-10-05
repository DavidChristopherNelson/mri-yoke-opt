"""All run parameters. Placeholders flagged with PLACEHOLDER; set per run.
Units: SI (m, T, A/m, kg, $)."""
from dataclasses import dataclass, field
import math

MU0 = 4e-7 * math.pi
NU0 = 1.0 / MU0


@dataclass
class Config:
    # ---- target ----
    B0: float = 0.159                 # target mean |B| in imaging volume [T]
    mean_tol: float = 1e-3            # ± tolerance on mean [T]
    ppm_max: float = 400.0            # (max-min)/mean over DSV surface [ppm]

    # ---- imaging volume (sphere, centred at origin) ----
    dsv_radius: float = 0.100         # PLACEHOLDER: 200 mm DSV
    clearance: float = 0.020          # air shell around DSV where no iron allowed [m]

    # ---- patient/bed keep-out envelope: box centred at origin, no iron or ferrite inside ----
    env_x: float = 0.400              # PLACEHOLDER full width [m]
    env_y: float = 0.400              # PLACEHOLDER full depth [m]
    env_z: float = 0.300              # PLACEHOLDER full height = pole gap [m]

    # ---- ferrite (design variable, magnetized +-z per element) ----
    Br_f: float = 0.40                # remanence [T]
    mu_r_f: float = 1.05
    ferrite_density: float = 4900.0   # kg/m^3
    ferrite_cost_per_kg: float = 3.0  # PLACEHOLDER $/kg
    C_fixed: float = 15000.0          # PLACEHOLDER fixed cost of the scanner [$]
    Hcj_cold: float = 250e3           # intrinsic coercivity at the coldest operating temperature [A/m]
    demag_frac: float = 0.8           # demagnetisation gate: H.m >= -demag_frac * Hcj_cold
    demag_weight: float = 10.0        # weight of the quadratic demagnetisation penalty

    # ---- design domain (1/8 octant box) and air box ----
    design_L: float = 0.450           # design box edge from origin [m]
    air_L: float = 1.000              # outer air box edge from origin [m]

    # ---- iron: Brauer model nu(|B|) = k1 exp(k2 |B|^2) + k3, generic low-carbon steel ----
    k1: float = 49.4
    k2: float = 1.46
    k3: float = 520.6
    iron_density: float = 7850.0      # kg/m^3
    iron_cost_per_kg: float = 2.0     # PLACEHOLDER $/kg

    # ---- mesh ----
    maxh_air: float = 0.12
    maxh_design: float = 0.035
    maxh_dsv: float = 0.025            # imaging region (envelope box)
    fe_order: int = 2

    # ---- solver ----
    newton_maxit: int = 25
    newton_tol: float = 1e-8
    reg_eps: float = 1e-8             # regularisation eps*nu0*(u,v), NGSolve tutorial 2.4
    linear_solver: str = "bddc"
    verbose_newton: bool = False      # log every Newton iteration of every forward solve

    # ---- optimisation (NGSolve tutorial 7.6 pattern) ----
    iter_max: int = 60
    kappa0: float = 0.1
    kappa_max: float = 1.0
    kappa_min: float = 1e-3
    kappa_up: float = 1.5
    kappa_down: float = 0.6
    ls_max_tries: int = 10
    ls_refine_gain: float = 0.05      # keep shrinking kappa after the first decrease only while J improves by this fraction
    dJ_rel_tol: float = 1e-4
    dJ_rel_count: int = 5
    time_budget_h: float = 0.0        # stop cleanly (checkpoint saved) before this much wall time is used; 0 = no limit
    threads: int = 0                  # NGSolve TaskManager threads; 0 = all (set when several runs share a machine)
    run_history: str = "results/run_history.csv"   # one row per finished run: size, machine, timing; feeds the ETA
    resume: str = ""                  # path to psi_latest.npy / psi_final.npy of an earlier run on the same mesh
    init: str = "hframe"              # initial design: "hframe" (iron plate + post + pole, ferrite slab) or "empty" (no field without ferrite: test only)
    mass_step_frac_fe: float = 0.01   # per step, iron added + iron removed <= this fraction of iron_ref_kg
    mass_step_frac_f: float = 0.01    # same for ferrite, fraction of ferrite_ref_kg
    iron_ref_kg: float = 300.0        # PLACEHOLDER reference masses for the step caps (~ expected mass, full magnet)
    ferrite_ref_kg: float = 200.0     # PLACEHOLDER
    w_misfit0: float = 1e3            # initial penalty weight on field misfit
    w_grow: float = 1.5               # multiply when constraints violated
    w_max: float = 1e8
    w_shrink: float = 1.2             # divide when satisfied
    harm_order: int = 6               # even harmonics of |B| in the DSV up to this degree get a Gauss-Newton correction
    gn_max_solves: int = 3            # forward solves per trial (predictor + correctors)
    gn_step_max: float = 0.3          # max trust radius: cap on the level-set shift per mode direction (psi, directions unit L2 norm)
    gn_lm: float = 1e-2               # Levenberg-Marquardt damping, relative to mean diag(M^T M); adapted per iteration
    gn_fd_eps: float = 0.02           # level-set shift used to differentiate the cut ratios
    sens_eps: float = 0.03            # floor of the sensitivity scaling, in rms units
    sens_power: float = 0.5           # scaling exponent: 1 = all regions move alike, 0 = raw sensitivity; < 1 keeps the ranking
    ls_max_fails: int = 3             # consecutive failed line searches before stopping

    # ---- initial guess: iron back plate + posts + pole plate, ferrite slab (1/8 octant boxes) ----
    slab_x: float = 0.300             # ferrite slab full width [m]
    slab_y: float = 0.300             # full depth [m]
    slab_t: float = 0.050             # thickness [m]
    slab_z0: float = 0.150            # lower face (>= env_z / 2) [m]
    plate_t: float = 0.040
    post_t: float = 0.050
    pole_t: float = 0.010

    # ---- output ----
    results_dir: str = "results"
    n_surface_pts: int = 600          # points on DSV octant surface for ppm metric
    viewer_h: float = 0.020           # marching-cubes grid spacing for viewer [m]
    slice_res: int = 120

    @property
    def z_slab1(self):
        return self.slab_z0 + self.slab_t

    @property
    def M_f(self):
        """Magnetization source of full ferrite, (Br_f / mu0) / mu_r_f [A/m]."""
        return self.Br_f * NU0 / self.mu_r_f
