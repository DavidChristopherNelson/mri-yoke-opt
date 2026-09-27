# Plan (revised 2026-09-27)

Supersedes the original "Initial Reduced Scope MRI Yoke/Bucking Magnet" brief. Decisions from review:
NGSolve primary, defaults win over brief, no randomness, no bucking magnets, solid iron, H-frame, 3D only.

## 1. Geometry and symmetry

- Netgen OCC. Bodies: air box, 2 pole magnets (fixed), imaging volume (fixed, sphere or ovaloid), design domain D = everything else inside the air box minus a clearance shell.
- 1/8 model: poles along z. Symmetry BCs for the A-formulation
  - z = 0 midplane: B normal to plane → natural BC (nothing imposed).
  - x = 0 and y = 0 planes: B tangential → n × A = 0 (Dirichlet).
  - outer box: n × A = 0. Box ≥ 5× yoke extent, or add a graded far-field region.
- Imaging volume meshed as its own solid with fine `maxh` so its surface is exact.

## 2. Physics (NGSolve tutorial 2.4 pattern)

```
fes = HCurl(mesh, order=3, dirichlet="outer|symx|symy", nograds=True)
a  += nu(B) * curl(u) * curl(v) * dx + eps * u * v * dx     # eps ~1e-8/mu0 regularisation, per tutorial
f  += M * curl(v) * dx("magnet")                             # M = Br/mu0 in magnet, along z
```
- Nonlinear ν(|B|) as a `BSpline`/`IfPos`-guarded CoefficientFunction; Newton via `a.AssembleLinearization(gfu.vec)`; guard |B| → 0 per NGSolve forum guidance.
- Linear solves: sparse direct (`pardiso` if available, else `sparsecholesky`) on the coarse mesh; switch to `CG` + `bddc` for production.
- Iron: AISI 1010 B-H table (FEMM material library values). Placeholder for roofing steel.
- Magnet: NdFeB N42, Br = 1.30 T, μr = 1.05 (placeholder; set per run).

## 3. Design representation and update (NGSolve tutorial 7.6 pattern)

- Level set ψ ∈ H1(order=1) on the fixed mesh; ψ < 0 ⇔ iron. Piecewise-constant `CutRatio` per element from `interpolations.InterpolateLevelSetToElems`.
- Material per element: ν_e = CutRatio · ν_iron(B) + (1 − CutRatio) · ν_air.
- Update: ψ ← (1−κ)ψ + κ·g/‖g‖_{L2}, κ₀ = 0.1, κ_max = 1; accept if J decreases (κ ← min(1.1κ, κ_max)) else κ ← 0.8κ, up to 10 tries.
- Objective driven by the optimizer:
  J(ψ) = w_f · ∫_Γ (|B| − B₀)² ds  +  w_c · ∫_D χ_iron dx
  Γ = imaging-volume surface, B₀ = 159 mT. w_c from iron $/kg × 7850 kg/m³; w_f tuned so the ppm constraint binds. Constraint handling by penalty first; augmented Lagrangian if penalty tuning is fragile.
- Stopping: iter_max (200 coarse) OR κ < 1e-3 after rejects OR |ΔJ|/J < 1e-4 for 5 consecutive accepted steps. Report actual mean and (max−min)/mean each iteration.

## 4. Sensitivity

- Adjoint: solve linearized system (transpose of Newton Jacobian at converged state) with RHS ∂J/∂A from the surface misfit term.
- Topological derivative in element e: TD_e ∝ (ν_in − ν_out) · P(ν_in, ν_out, tangent) · curl A_fwd · curl A_adj + w_c (polarization-tensor form; sign flips between iron→air and air→iron per tutorial 7.6 NegPos/PosNeg).
- **Default not sufficient here**: NGSolve ships TD machinery for linear transmission problems (tutorial 7.6/7.7); the exact 3D nonlinear-magnetostatics TD (Gangl–Sturm 2019) needs an exterior auxiliary problem per point. We use the linearized (tangent ν at local |B|) polarization tensor instead. Acceptable because iron in a yoke operates mostly below saturation; revisit if final designs saturate.

## 5. Field evaluation

- Sample B on Γ via boundary integration of `BoundaryFromVolumeCF(curl(gfA))`, plus point evaluation at a fixed Fibonacci-sphere set mapped onto Γ (works for ovaloids) for the post-hoc (max−min)/mean metric.
- Discretization check: refine Γ mesh + raise order until reported ppm changes < 20 ppm. Target: solver error ≪ 400 ppm.
- **Default not sufficient here**: pointwise B from curl A on tets loses an order; if ppm noise is too high, switch the metric to a spherical-harmonic fit of Bz on Γ (only valid for a spherical Γ) or raise `order` to 4.

## 6. Compute

Coarse laptop mesh (~100k elements, order 2) for all development. Production (few M elements, order 3, hundreds of iterations) later, out of scope.

## 7. Software

| tool | role |
|---|---|
| NGSolve / Netgen (pip `ngsolve`) | mesh, FE, Newton, TD/level-set loop |
| numpy / scipy | B-H interpolation, metrics |
| ParaView | 3D viz via VTKOutput |
| Firedrake / Fireshape / Gmsh | dropped (NGSolve defaults cover them) |

## 8. Deviations from the original brief (defaults won)

| brief | now | why |
|---|---|---|
| scalar potential | vector potential A, HCurl | NGSolve tutorial default |
| Gmsh | Netgen OCC | NGSolve default; single tool |
| stochastic BESO, 1% cap, annealing | level-set + TD fixed-point with line search | NGSolve tutorial 7.6; deterministic, uses gradient |
| body-fitted remesh each iter | fixed mesh, cut-ratio material | tutorial 7.6; free-floating iron nucleates natively |
| laminated anisotropic iron | solid isotropic 1010 steel | scoped out |
| (max−min)/mean as objective | least-squares surface misfit as objective, (max−min)/mean checked post-hoc | non-smooth metric unusable for gradients |
| Firedrake + pyadjoint | NGSolve | user choice |

## 9. Open questions

1. Imaging volume: sphere or ovaloid for first run, and dimensions?
2. Pole magnet: material grade, dimensions, gap?
3. Iron $/kg to use for the cost weight?
4. Repo license (none added yet)?
5. Air-box size / far-field treatment acceptable as above?
