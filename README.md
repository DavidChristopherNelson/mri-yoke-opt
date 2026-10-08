# mri-yoke-opt

Topology optimization of a low-field (159.2 mT) permanent-magnet MRI magnet: where to put iron and ferrite.
3D from day one. NGSolve primary. Gradient-based (level sets + adjoint sensitivities); deterministic given the seed.

**Plan X** (PLAN.md section 10): iron and ferrite are both free; the objective is the cost per good imaging voxel,

    F = (C_fe + C_f + C_fixed) / N_green.

Status: pipeline runs end to end on the coarse laptop mesh; gradients verified against finite differences. On that mesh the FE noise (1–3 mT) is larger than the field band (±0.15 mT), so coarse runs validate the pipeline, not the design. Placeholders in `mriyoke/config.py` (envelope 400×400×300 mm, $/kg, reference masses) need real values.

## Run

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
caffeinate -s -i .venv/bin/python scripts/run_coarse.py iter_max=150 results_dir=results/x   # any Config field overridable as key=value
caffeinate -s -i .venv/bin/python scripts/run_coarse.py init=noise:3 results_dir=results/n3  # noise seed 3
caffeinate -s -i .venv/bin/python scripts/run_fine.py                                        # finer mesh, results/fine
# caffeinate: macOS otherwise sleeps mid-run. verbose_newton=1 logs every Newton iteration.
```

Cloud (Azure spot VM, Standard_F16als_v7 in eastus2, every iteration persisted to Azure Blob Storage): see `cloud/azure/README.md`, including account setup from scratch. Runs there are created, followed, paused and cancelled in the cloud dashboard (`cloud/azure/azure.sh dashboard`, http://localhost:8770/).

Resume: `auto_resume=1` continues an interrupted run in the same `results_dir` (iteration numbering, history and viewer kept); finished runs are skipped.

Multi-start (3 H-frame seeds + N noise seeds, then a summary):

```
caffeinate -s -i .venv/bin/python scripts/multistart.py ms1 --n 8 --batch 4 --hours 3.5 iter_max=150
```

Results in `results/ms1/<run>/`; `results/ms1/summary.csv` (final F, cost, N_green, iron kg, ferrite kg, iterations, stop reason), `iou_iron.csv`, `iou_ferrite.csv` (pairwise intersection-over-union of the final masks, for clustering). `--summary-only` rebuilds the summary.

Sessions (hand-picked variants side by side, fixed time budget, live read-out):

```
.venv/bin/python scripts/session.py overnight --hours 10 "base" "gmax20:G_max=0.02" "step3:mass_step_frac_f=0.03"
```

Results in `results/<session>/<variant>/`; table (state, iteration, s/it, mean field, N_green, F, expected and latest finish) refreshes in the terminal and in `results/<session>/status.txt`. Threads are split evenly between variants. A run that hits the budget stops cleanly and can be continued with `resume=results/.../psi_latest.npy` (the ferrite level set and the polarities next to it are picked up). Every finished run appends one row to `results/run_history.csv`; the expected-finish estimate uses the iteration counts of earlier comparable runs there.

Checks:

```
.venv/bin/python scripts/test_forward.py Br_f=1.3     # one forward solve on the H-frame start
.venv/bin/python scripts/test_gradient.py 1e-2        # adjoint vs finite differences: iron, ferrite, polarity source
.venv/bin/python scripts/precheck.py T_scan=600       # SNR precondition (the optimiser never uses SNR)
```

Outputs per iteration in `results/<run>/`:

| file | view with |
|---|---|
| `viewer.html` | any browser: iron (grey), ferrite coloured by m_z (red +z, white transverse, blue −z) with black direction arrows, largest green component (translucent green), envelope (blue box), corridor (dashed); axes x red, y green, z blue; slider/play over all iterations |
| `iter_XXXX.png` | y=0 slice: \|B\| with the band edges, iron fraction, ferrite fraction × m_z with direction arrows |
| `iter_XXXX.vtu` | ParaView: psi, psi_f, iron and ferrite fractions, magnetization direction m, B, \|B\| on the 1/8 mesh (use Reflect filter for full) |
| `history.png`, `history.csv` | F, F_smooth, N_green, N_smooth, blob mean field, cost, cost_fe, cost_f, masses, demag_frac, residual, J per iteration |
| `run.log` | line-search trace, `eta:` line per iteration |
| `status.json` | current state, s/it, expected / latest finish |
| `psi_latest.npy`, `psif_latest.npy`, `mdir_latest.npy` | checkpoint after every accepted step (iron level set, ferrite level set, directions) |
| `*_final.npy`, `mask_final.npy` | final state; material mask on the viewer grid (0 air, 1 iron, 2 ferrite m_z ≥ 0, 3 ferrite m_z < 0) |

## Problem

Design domain: the 1/8 design box minus the patient/bed keep-out envelope and minus the access corridor (the envelope's x–z footprint extruded along y through the whole box, `corridor=1`). Anywhere in it an element can be air, iron or ferrite; every ferrite element has its own magnetization direction (a unit vector). Free-floating pieces are allowed; no manufacturability constraint yet.

A voxel of the imaging grid (3 mm, filling the envelope) is **green** when at its centre

| test | value with the defaults |
|---|---|
| \| \|B\| − B_c \| ≤ dB_band / 2, B_c = 159.2 mT | ± 0.15 mT |
| \| ∂\|B\|/∂r \| ≤ κ_s · dB_band / (2 δ dx_img), r = readout axis (y) | 4 mT/m |

dB_band = min(ism_fraction × 0.70 mT, 2 δ dx_img G_max) comes from hardware (ISM band share, maximum readout gradient); the run log states which limit binds. N_green = size of the largest connected green component.

Start designs: `init=hframe:thin|medium|thick` (iron back plate + post, ferrite slab of 30 / 50 / 80 mm magnetized +z) or `init=noise:<seed>` (blurred noise, 20 % iron and 10 % ferrite; each ferrite blob gets one random direction, which the elements then change independently).

Each accepted step moves (adds + removes) at most 5 % of a reference mass per material: `mass_step_frac_fe` × `iron_ref_kg` (15 kg) and `mass_step_frac_f` × `ferrite_ref_kg` (10 kg); the caps are Config fields (1 % in the original brief, raised 2026-10-05 because the field then gained only ~1 mT per iteration). Dense noise seeds (~900 kg iron, ~270 kg ferrite) shed mass slowly even so: on the order of a hundred iterations or more before they get near the band (~700 at 1 %).

## Method (NGSolve default patterns)

Where an NGSolve default clashes with the brief, the default wins. Deviations flagged in `PLAN.md` (8 and 10.5).

- **Geometry/mesh**: Netgen OCC. 1/8 symmetry. Envelope box and the projection sphere at its centre are their own regions.
- **Physics**: magnetostatics, vector potential A in `HCurl(order=2, nograds=True)`, nonlinear iron (Brauer) via damped Newton, CG + BDDC (tutorial 2.4). Ferrite: linear, μr 1.05, magnetization source cr_f · (Br_f/μ0)/μr_f · m, m the element's unit direction.
- **Design representation**: two level sets on a fixed mesh (ψ iron, ψ_f ferrite, ferrite wins overlaps), exact tet cut ratios, geometric mix of the reluctivities in cut elements; magnetization direction per element.
- **Objective**: J = ln(cost / N_smooth) + demagnetisation penalty. N_smooth = sum over voxels of two sigmoids (band, slope) of the even-harmonic polynomial projection of \|B\| (degree ≤ 8, 15 modes) in the projection sphere; sigmoid widths annealed 1 → 0.05 of the band over 100 iterations, never narrower than the present field error.
- **Update**: fixed-point ψ ← (1−κ)ψ + κ g + ν d for both level sets (tutorial 7.6 pattern), g = scaled sensitivity of J, κ by line search; d = sensitivity of the mean field of the green blob, ν a Gauss–Newton correction that holds that mean at B_c.
- **Sensitivity**: adjoint, exact discrete gradient w.r.t. the per-element iron and ferrite fractions (material + source term); one adjoint solve per mode, one Jacobian.
- **Direction**: dJ/dm_e = cr_f M_f ∫_e curl λ, so the best direction is −∫_e curl λ normalised. An element without ferrite takes it outright; existing ferrite turns towards it by at most `dir_rot_max` (0.2 rad) per step, inside the line search.
- **Demagnetisation gate**: fraction of ferrite with H·m < −0.8 Hcj(T_cold) reported as `demag_frac`, quadratic penalty when violated.
- **Stopping**: `iter_max`, time budget, 3 consecutive failed line searches, or relative change of F < 1e-4 over 5 iterations once the annealing is done.

## Out of scope (for now)

Stochastic steps inside the loop · bucking coils · laminations/anisotropy · manufacturability/connectivity constraint · 2D or axisymmetric models · cloud compute (options surveyed in `reports/`).

## Layout

```
mriyoke/config.py       all parameters (dataclass); dB_band and s_max derived from hardware fields
mriyoke/geometry.py     OCC 1/8 octant: air, design box, envelope, projection sphere; symmetry face names
mriyoke/physics.py      HCurl A-formulation, Brauer nu(|B|), iron/ferrite/air mix, ferrite source, damped Newton, CG+BDDC, adjoint solve
mriyoke/levelset.py     two level sets (H1 order 1), exact tet cut ratio, H-frame and noise seeds, capped update
mriyoke/imaging.py      imaging voxel grid: exact green count (connected components), harmonic basis, smooth count
mriyoke/sensitivity.py  adjoint gradients of the projection modes and the demagnetisation penalty; cost gradient
mriyoke/metrics.py      masses, costs, centre field
mriyoke/export.py       VTK, slice PNG, marching-cubes frames + self-contained three.js viewer
mriyoke/optimize.py     main loop
mriyoke/timing.py       ETA, status.json, run_history.csv
scripts/run_coarse.py   entry point (coarse mesh);  run_fine.py: finer mesh;  session.py: variants side by side;
scripts/multistart.py   seeds + summary;  precheck.py: SNR precondition;  test_*.py: checks
```

See `PLAN.md` for details and decisions.
