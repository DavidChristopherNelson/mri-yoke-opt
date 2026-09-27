# mri-yoke-opt

Topology optimization of the iron yoke for a low-field (159 mT) H-frame permanent-magnet MRI scanner.
3D from day one. NGSolve primary. Gradient-based (level-set + topological derivative), no stochastic steps.

Status: coarse-mesh pipeline runs end to end on a laptop. Placeholders in `mriyoke/config.py` (DSV 200 mm, 300×300×50 mm N42 magnets, 300 mm gap, $2/kg iron) need real values.

## Run

```
python3 -m venv .venv && .venv/bin/pip install ngsolve numpy scipy scikit-image matplotlib
.venv/bin/python scripts/run_coarse.py iter_max=60 results_dir=results/coarse   # any Config field overridable as key=value
```

Outputs per iteration in `results/<run>/`:

| file | view with |
|---|---|
| `viewer.html` | any browser: 3D iron surface (mirrored to full magnet), magnets, DSV; slider/play over all iterations, stats per frame |
| `iter_XXXX.png` | y=0 slice: \|B\| map and iron fraction |
| `iter_XXXX.vtu` | ParaView: psi, iron fraction, B vector, \|B\| on the 1/8 mesh (use Reflect filter for full) |
| `history.png`, `history.csv` | ppm, mean B, iron kg, J, w per iteration |
| `run.log` | line-search trace |

## Problem

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
- **Design representation**: level-set ψ on a fixed background mesh, element cut-ratio interpolation of ν(B) between air and iron. NGSolve tutorial 7.6 pattern.
- **Update**: fixed-point ψ ← (1−κ)ψ + κ g/‖g‖ with g the (generalized) topological derivative, Armijo-style κ line search (κ×1.1 on accept, ×0.8 on reject), as in tutorial 7.6. Free-floating iron nucleates naturally.
- **Sensitivity**: topological derivative from forward field × adjoint field (adjoint sourced by the surface-misfit objective). Nonlinear 3D TD approximated with the tangent-permeability polarization tensor (see PLAN.md).
- **Stopping**: `iter_max` plus step-rejection (κ below κ_min) plus relative objective change < 1e-4 over 5 iters.
- **Field evaluation**: B sampled on the imaging-volume surface directly (works for sphere and ovaloid). Smooth surrogate ∫(|B|−B₀)² ds drives the optimizer; the real (max−min)/mean and mean constraints are checked post-hoc every iteration.
- **Visualization**: NGSolve webgui during dev, VTK export → ParaView for 3D.

## Out of scope (for now)

Stochastic/annealing steps · bucking magnets · laminations/anisotropy · manufacturability/connectivity constraint · 2D or axisymmetric models · looping over imaging volumes / pole geometries · cloud compute.

## Layout

```
mriyoke/config.py       all parameters (dataclass)
mriyoke/geometry.py     OCC 1/8 octant: air, design box, clearance shell, DSV, magnet; symmetry face names
mriyoke/physics.py      HCurl A-formulation, Brauer nu(|B|), damped Newton, CG+BDDC, adjoint solve
mriyoke/levelset.py     psi (H1 order 1), exact tet cut-ratio, initial H-frame guess, update/line-search helpers
mriyoke/sensitivity.py  adjoint gradient of field misfit and iron cost w.r.t. per-element iron fraction
mriyoke/metrics.py      DSV-surface mean / (max-min)/mean, iron mass and cost
mriyoke/export.py       VTK, slice PNG, marching-cubes frames + self-contained three.js viewer
mriyoke/optimize.py     main loop (tutorial 7.6 fixed-point + line search, adaptive penalty)
scripts/run_coarse.py   entry point;  scripts/test_forward.py, test_newton.py: stage checks
```

See `PLAN.md` for details and decisions.
