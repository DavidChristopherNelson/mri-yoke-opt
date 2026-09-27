# mri-yoke-topopt

Topology optimization of the iron yoke for a low-field (159 mT) H-frame permanent-magnet MRI scanner.
3D from day one. NGSolve primary. Gradient-based (level-set + topological derivative), no stochastic steps.

Status: plan only. No code yet.

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

## Layout (planned)

```
geometry/    OCC H-frame, magnets, imaging volume, air box, symmetry BCs
physics/     A-formulation, B-H curve, Newton solve
topopt/      level set, cut-ratio, topological derivative, line search, stopping
eval/        surface sampling, mean / ppm metrics, VTK export
scripts/     run_coarse.py (laptop debug), run_production.py (later)
```

See `PLAN.md` for details and decisions.
