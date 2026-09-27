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

    # ---- pole magnet: rectangular prism, magnetized +z, one per side ----
    mag_x: float = 0.300              # PLACEHOLDER full width [m]
    mag_y: float = 0.300              # PLACEHOLDER full depth [m]
    mag_t: float = 0.050              # PLACEHOLDER thickness [m]
    gap: float = 0.300                # PLACEHOLDER pole-face to pole-face gap [m]
    Br: float = 1.30                  # PLACEHOLDER NdFeB N42 remanence [T]
    mu_r_mag: float = 1.05

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
    maxh_dsv: float = 0.025
    maxh_mag: float = 0.025
    fe_order: int = 2

    # ---- solver ----
    newton_maxit: int = 25
    newton_tol: float = 1e-8
    reg_eps: float = 1e-8             # regularisation eps*nu0*(u,v), NGSolve tutorial 2.4
    linear_solver: str = "bddc"

    # ---- optimisation (NGSolve tutorial 7.6 pattern) ----
    iter_max: int = 60
    kappa0: float = 0.1
    kappa_max: float = 1.0
    kappa_min: float = 1e-3
    kappa_up: float = 1.1
    kappa_down: float = 0.8
    ls_max_tries: int = 10
    dJ_rel_tol: float = 1e-4
    dJ_rel_count: int = 5
    w_misfit0: float = 1e3            # initial penalty weight on field misfit
    w_grow: float = 1.5               # multiply when constraints violated
    w_max: float = 1e8
    w_shrink: float = 1.2             # divide when satisfied

    # ---- initial iron guess: back plate + posts + pole plate (1/8 octant boxes) ----
    plate_t: float = 0.040
    post_t: float = 0.050
    pole_t: float = 0.010

    # ---- output ----
    results_dir: str = "results"
    n_surface_pts: int = 600          # points on DSV octant surface for ppm metric
    viewer_h: float = 0.020           # marching-cubes grid spacing for viewer [m]
    slice_res: int = 120

    @property
    def z_mag0(self):
        return self.gap / 2

    @property
    def z_mag1(self):
        return self.gap / 2 + self.mag_t
