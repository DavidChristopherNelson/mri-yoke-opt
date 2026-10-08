"""All run parameters. Placeholders flagged with PLACEHOLDER; set per run.
Units: SI (m, T, A/m, kg, $)."""
from dataclasses import dataclass, field
import math

MU0 = 4e-7 * math.pi
NU0 = 1.0 / MU0


@dataclass
class Config:
    # ---- imaging: a voxel is green when | |B| - B_c | <= dB_band / 2 and | d|B|/dr | <= s_max at its centre ----
    B_c: float = 0.1592               # centre field [T] (6.78 MHz ISM band)
    dx_img: float = 0.003             # imaging voxel edge [m]; the voxel grid fills the envelope box
    readout_axis: str = "y"           # r in d|B|/dr
    ism_band: float = 0.70e-3         # width of the ISM band in field units [T]
    ism_fraction: float = 0.5         # usable fraction of it
    delta: float = 5.0                # tolerated readout displacement in voxels (5: field-map corrected; 1 if not)
    G_max: float = 0.010              # maximum readout gradient [T/m]
    kappa_s: float = 0.4              # allowed static slope as a fraction of the readout gradient
    anneal_start: float = 1.0         # sigmoid widths of the smooth count, as fractions of dB_band and s_max:
    anneal_end: float = 0.05          #   geometric continuation from anneal_start to anneal_end
    anneal_iters: int = 100           #   over this many iterations
    proj_radius: float = 0.10         # the polynomial projection and the smooth count live in this sphere (own mesh region);
                                      #   0 = whole envelope box (does not converge there: sources touch the box, see PLAN.md)
    width_shrink_max: float = 1.5     # the smooth count's sigmoid widths shrink by at most this factor per iteration
    blob_min_voxels: int = 8          # smaller green blobs (inside the projection sphere) do not anchor the mean-field correction
    blob_fallback_r: float = 0.05     # mean-field correction acts on this sphere while there is no green voxel [m]

    # ---- patient/bed keep-out envelope: box centred at origin, no iron or ferrite inside ----
    env_x: float = 0.400              # PLACEHOLDER full width [m]
    env_y: float = 0.400              # PLACEHOLDER full depth [m]
    env_z: float = 0.300              # PLACEHOLDER full height = pole gap [m]
    corridor: bool = True             # keep the envelope's x-z footprint free of material along the whole y axis (patient access)

    # ---- ferrite (design variable; each element has its own magnetization direction, a unit vector) ----
    Br_f: float = 0.40                # remanence [T]
    mu_r_f: float = 1.05
    ferrite_density: float = 4900.0   # kg/m^3
    ferrite_cost_per_kg: float = 3.0  # PLACEHOLDER $/kg
    C_fixed: float = 2000.0           # fixed cost of the scanner [$]
    Hcj_cold: float = 250e3           # intrinsic coercivity at the coldest operating temperature [A/m]
    demag_frac: float = 0.8           # demagnetisation gate: H.m >= -demag_frac * Hcj_cold
    demag_weight: float = 10.0        # weight of the quadratic demagnetisation penalty
    dir_rot_max: float = 0.2          # existing ferrite turns towards its best direction by at most this angle per step [rad]

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
    kappa_min: float = 1e-3           # line search gives up below this fraction of the mass-capped step
    kappa_up: float = 1.5
    kappa_down: float = 0.6
    ls_max_tries: int = 10
    ls_refine_gain: float = 0.05      # keep shrinking kappa after the first decrease only while J improves by this fraction
    dJ_rel_tol: float = 1e-4
    dJ_rel_count: int = 5
    time_budget_h: float = 0.0        # stop cleanly (checkpoint saved) before this much wall time is used; 0 = no limit
    threads: int = 0                  # NGSolve TaskManager threads; 0 = all (set when several runs share a machine)
    run_history: str = "results/run_history.csv"   # one row per finished run: size, machine, timing; feeds the ETA
    it_offset: int = 0                # iteration number of the resume checkpoint (continues numbering, annealing, history)
    resume: str = ""                  # path to psi_latest.npy / psi_final.npy of an earlier run on the same mesh
    init: str = "hframe"              # initial design: "hframe[:thin|medium|thick]" (iron plate + post + pole, ferrite slab of
                                      #   30 / 50 / 80 mm; plain "hframe" uses slab_t), "noise:<seed>" (blurred-noise iron and
                                      #   ferrite), "empty" (nothing: no field, test only)
    h_seed: float = 0.010             # noise seeds: pitch of the noise grid [m]
    ell_seed: float = 0.040           #   correlation length of the Gaussian blur [m]
    seed_iron_frac: float = 0.20      #   iron volume fraction of the design box
    seed_ferrite_frac: float = 0.10   #   ferrite volume fraction (ferrite wins overlaps)
    mass_step_frac_fe: float = 0.05   # per step, iron added + iron removed <= this fraction of iron_ref_kg
    mass_step_frac_f: float = 0.05    # same for ferrite, fraction of ferrite_ref_kg
    iron_ref_kg: float = 300.0        # PLACEHOLDER reference masses for the step caps (~ expected mass, full magnet)
    ferrite_ref_kg: float = 200.0     # PLACEHOLDER
    harm_order: int = 8               # |B| in the envelope is projected on the even harmonic polynomials up to this degree
    gn_max_solves: int = 3            # forward solves per trial (predictor + correctors)
    gn_step_max: float = 0.3          # max trust radius: cap on the level-set shift along the mean-field direction (psi, directions unit L2 norm)
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
    viewer_h: float = 0.020           # marching-cubes grid spacing for viewer [m]
    slice_res: int = 120

    @property
    def z_slab1(self):
        return self.slab_z0 + self.slab_t

    @property
    def M_f(self):
        """Magnetization source of full ferrite, (Br_f / mu0) / mu_r_f [A/m]."""
        return self.Br_f * NU0 / self.mu_r_f

    @property
    def dB_band(self):
        """Usable field band [T], from hardware: ISM band share or what the readout gradient can encode."""
        return min(self.ism_fraction * self.ism_band, 2 * self.delta * self.dx_img * self.G_max)

    @property
    def band_limit(self):
        return "ISM band" if self.ism_fraction * self.ism_band <= 2 * self.delta * self.dx_img * self.G_max else "G_max"

    @property
    def s_max(self):
        """Largest tolerated |d|B|/dr| [T/m]."""
        return self.kappa_s * self.dB_band / (2 * self.delta * self.dx_img)
