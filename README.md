# mri-yoke-opt

Topology optimization of the iron yoke for a low-field (159 mT) H-frame permanent-magnet MRI scanner.
3D from day one. NGSolve primary. Gradient-based (level-set + topological derivative), no stochastic steps.

Status: coarse-mesh pipeline runs end to end on a laptop and converges (mean field met, homogeneity limited by the coarse mesh at ~6e4 ppm; see PLAN.md 8c). Placeholders in `mriyoke/config.py` (DSV 200 mm, 300×300×50 mm N42 magnets, 300 mm gap, $2/kg iron) need real values.

## Run

```
python3 -m venv .venv && .venv/bin/pip install ngsolve numpy scipy scikit-image matplotlib
caffeinate -s -i .venv/bin/python scripts/run_coarse.py iter_max=60 results_dir=results/coarse   # any Config field overridable as key=value
# caffeinate: macOS otherwise sleeps mid-run. verbose_newton=1 logs every Newton iteration.
caffeinate -s -i .venv/bin/python scripts/run_fine.py                # finer mesh (49k tets, 263k dofs), ~5x cost per solve, results/fine, ~4-6 h
```

Sessions (several variants side by side, fixed time budget, live read-out):

```
.venv/bin/python scripts/session.py overnight --hours 10 "base" "clear30:clearance=0.03" "step3:mass_step_frac=0.03"
```

Results in `results/<session>/<variant>/`; table (state, iteration, s/it, expected and latest finish as wall-clock time) refreshes in the terminal and in `results/<session>/status.txt`. Threads are split evenly between variants. A run that hits the budget stops cleanly and can be continued with `resume=results/.../psi_latest.npy`. Every finished run appends one row (mesh size, machine, threads, iterations, solves, seconds) to `results/run_history.csv`; the expected-finish estimate uses the iteration counts of earlier comparable runs there, so it is "unknown" until one such run has ended by itself.

Outputs per iteration in `results/<run>/`:

| file | view with |
|---|---|
| `viewer.html` | any browser: 3D iron surface (mirrored to full magnet), magnets, DSV; slider/play over all iterations, stats per frame |
| `iter_XXXX.png` | y=0 slice: \|B\| map and iron fraction |
| `iter_XXXX.vtu` | ParaView: psi, iron fraction, B vector, \|B\| on the 1/8 mesh (use Reflect filter for full) |
| `history.png`, `history.csv` | ppm, mean B, iron kg, J, w per iteration |
| `run.log` | line-search trace, `eta:` line per iteration |
| `status.json` | current state, s/it, expected / latest finish |
| `psi_latest.npy` | checkpoint after every accepted step |
| `psi_final.npy` | final level-set nodal values |

## Problem

Start: no iron (`init=empty`; `init=hframe` gives the old plate + post + pole guess). Each step adds + removes at most `mass_step_frac` (1 %) of `mass_step_ref_kg` (300 kg placeholder → 3 kg/step), so the yoke is built up slowly.

Fixed per run: pole magnet geometry + magnetization, imaging volume (sphere or ovaloid) size + location.
Design variable: where iron is, anywhere outside the pole magnets and imaging volume.
Free-floating iron is allowed (supported by non-magnetic structure); no manufacturability constraint yet.

Objective: minimize iron cost (volume × $/kg) subject to

| constraint | value |
|---|---|
| mean \|B\| over imaging-volume surface | 159 mT ± 1 mT |
| homogeneity, (max − min) / mean over surface | ≤ 400 ppm |

## Method (NGSolve default patterns)

Where an NGSolve default clashes with the original brief, the default wins. Deviations flagged in `PLAN.md`.

- **Geometry/mesh**: Netgen OCC (`netgen.occ`), not Gmsh. 1/8 H-frame symmetry. Imaging volume is its own OCC solid so its surface is meshed exactly, with fine `maxh`.
- **Physics**: magnetostatics, vector potential A in `HCurl(order=2..3, nograds=True)`, permanent magnet as magnetization source `M·curl(v)`, nonlinear B-H via Newton (`AssembleLinearization`). This is NGSolve tutorial 2.4; scalar potential from the brief dropped in favour of the default.
- **Iron**: solid, isotropic, AISI 1010 low-carbon steel B-H curve (placeholder for roofing steel; laminations out of scope).
- **Design representation**: level-set ψ on a fixed background mesh, exact tet cut ratio; cut elements use a geometric mix ν0^(1−cr)·ν_iron^cr (the linear mix of tutorial 7.6 is unusable at iron/air contrast, see PLAN.md 8c).
- **Update**: fixed-point ψ ← (1−κ)ψ + κ g + Σ ν_k d_k. g = scaled sensitivity of J (tutorial 7.6 pattern), κ by line search (backtrack to first decrease, then refine). d_k = sensitivities of the low-order harmonic modes of |B| in the DSV (mean, z², … up to degree `harm_order`); ν is a Gauss–Newton correction that cancels those modes, they are the stiff directions that made the plain update zigzag. Free-floating iron nucleates naturally.
- **Sensitivity**: adjoint, exact discrete gradient w.r.t. the per-element iron fraction (verified against finite differences); one adjoint solve for the misfit plus one per mode, all with the same Newton Jacobian.
- **Stopping**: `iter_max`, or 3 consecutive failed line searches, or relative objective change < 1e-4 over 5 iters.
- **Field evaluation**: B sampled on the imaging-volume surface directly (works for sphere and ovaloid). Smooth surrogate ∫(|B|−B₀)² dx over the imaging volume drives the optimizer; the real (max−min)/mean and mean constraints are checked post-hoc every iteration.
- **Visualization**: NGSolve webgui during dev, VTK export → ParaView for 3D.

## Out of scope (for now)

Stochastic/annealing steps · bucking magnets · laminations/anisotropy · manufacturability/connectivity constraint · 2D or axisymmetric models · looping over imaging volumes / pole geometries · cloud compute.

## Layout

```
mriyoke/config.py       all parameters (dataclass)
mriyoke/geometry.py     OCC 1/8 octant: air, design box, clearance shell, DSV, magnet; symmetry face names
mriyoke/physics.py      HCurl A-formulation, Brauer nu(|B|), geometric cut-element mix, damped Newton, CG+BDDC, adjoint solve
mriyoke/levelset.py     psi (H1 order 1), exact tet cut-ratio, initial H-frame guess, scaled update + mode directions
mriyoke/sensitivity.py  adjoint gradients of field misfit, DSV harmonic modes and iron cost w.r.t. per-element iron fraction
mriyoke/metrics.py      DSV-surface mean / (max-min)/mean, iron mass and cost
mriyoke/export.py       VTK, slice PNG, marching-cubes frames + self-contained three.js viewer
mriyoke/optimize.py     main loop (tutorial 7.6 fixed-point + Gauss-Newton mode correction + line search, adaptive penalty)
scripts/run_coarse.py   entry point (coarse mesh);  scripts/run_fine.py: finer mesh, 250 iterations max;  scripts/test_forward.py, test_newton.py: stage checks
```

See `PLAN.md` for details and decisions.
