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

## 8b. Implementation notes (2026-09-27, what was actually built)

- Objective uses the *volume* misfit ∫_dsv(|B|−B₀)² dx (smooth, cheap with HCurl); the surface (max−min)/mean is computed post-hoc from ~600 points on the DSV octant surface. Equivalent for a source-free sphere.
- Sensitivity is the exact discrete gradient of the cut-ratio-interpolated problem (adjoint with the Newton Jacobian), used directly as the level-set update direction g; the polarization-tensor TD scaling was dropped (Gangl–Sturm 2022 unified discrete sensitivity). Cost gradient = vol_e/V_design.
- Constraints via adaptive penalty: J = w·f + c, w×1.5 while violated (capped 1e8), w/1.2 when satisfied.
- Iron B-H: Brauer ν(|B|)=k1·exp(k2|B|²)+k3, k=(49.4, 1.46, 520.6) (μr≈1400 initial), clamped below ν0. Swap for a 1010 table later.
- Linear solver: CG + BDDC (tutorial 2.4); direct sparsecholesky also works. Newton residual is projected onto free dofs (otherwise Dirichlet-dof residual masks convergence).
- Coarse mesh (~20k tets, order 2, ~100k dofs): discretization error ~1e4 ppm, so the 400 ppm target is NOT resolvable on this mesh. Coarse runs validate the pipeline and the topology trend only.

## 8c. Optimizer fixes (2026-10-03)

First coarse run (commit e3d2940) did not work: mean B flipped 125 ↔ 190 mT every iteration, κ collapsed to 1e-3, penalty weight ran to its cap, ppm stuck at 2.4e5. Adjoint gradient was checked against finite differences and is correct; the causes were in the formulation:

1. **Material mix in cut elements (default not sufficient here)**: tutorial 7.6 mixes the coefficient linearly with the cut ratio. For ν with iron/air contrast ~1400 that makes a half-filled element air-like (μr ≈ 2) and squeezes the whole transition into cr > 0.99: the response to a moving interface is extremely nonlinear (FD check at δcr = 1e-2 was 35 % off the gradient). Now geometric mix ν = ν0^(1−cr) · ν_iron^cr (FD at 1e-2 within 6 %; cold Newton 8 its instead of 11 heavily damped).
2. **Stiff field modes**: misfit ≈ Σ (low-order harmonic coefficients of |B| in the DSV)²; mean, z², … are each far stiffer than the rest of the design space, so the plain fixed-point step zigzags. Now a Gauss–Newton correction rides on the tutorial update: ψ_new = (1−κ)ψ + κ g + Σ_k ν_k d_k. d_k = sensitivity of mode k (one extra adjoint solve each, same Jacobian), mode basis = L2(DSV)-orthonormal even harmonic polynomials up to degree `harm_order` (6 → 10 modes; they hold > 99.9 % of the initial misfit). ν from a damped least-squares on a linear model (mode sensitivities × finite-difference cut-ratio response, no field solve), then corrected with the true mode errors after each forward solve (≤ `gn_max_solves`); a step that does not improve J halves the trust radius on ν. LM damping and trust radius adapted per iteration. `harm_order` 8 was tried on the coarse mesh: stable, not better (117 DSV elements do not resolve those modes); 6 is the default.
3. **Sensitivity scaling**: sensitivities near the DSV are orders of magnitude larger than at the return yoke. All directions are multiplied by one positive field s = 1/√(ĝ² + ĝ_mean² + ε²) (sign of the optimality condition unchanged), ψ re-normalized in L2(design) after each accepted step.
4. **Line search**: tutorial rule (first decrease after ×0.8 backtracking) lands at the break-even step on the far wall of the valley. Now backtrack ×0.6 to the first decrease, then keep shrinking while J improves; ×1.5 growth after acceptance. A failed line search raises the LM damping and retries (stop after `ls_max_fails`).

5. **Newton stop test**: convergence was relative to the warm-start residual. After a small design change that residual is already near round-off, so the 1e-8 drop was unreachable and the solve burned all 25 Newton × 400 CG iterations. Now relative to the source residual (A = 0). Non-converged solves reject the trial.

Result on the same coarse mesh (`results/coarse`, stops at iteration 22 after 3 failed line searches, last accepted = 19, ~20 s/iteration once near the optimum):

| | old run (it 35) | new run (it 19) |
|---|---|---|
| mean \|B\| on DSV surface | 159.2 mT, oscillating | 159.02 mT, inside ±1 mT from it 4 |
| normalized misfit f | 6.4e-4 | 1.2e-5 |
| (max−min)/mean on surface | 2.5e5 ppm | 5.7e4 ppm |
| low-order mode error ‖ρ‖ | – | 1e-4 |
| iron | 179 kg | 222 kg |

What is left is not low-order: degree ≤ 6 content of the final field is ~2e3 ppm (20-iteration check run); the rest is degree ≥ 8 content from iron 20 mm off the DSV surface plus mesh noise (5e3–1e4 ppm even at r = 0.5 R). 400 ppm remains out of reach of this mesh (see 8b), and with the placeholder geometry (200 mm DSV in a 300 mm gap) it may be out of reach physically. Next levers: finer DSV/near-DSV mesh, then higher `harm_order`, larger `clearance`. Cost only acts as a tie-breaker while the ppm constraint is violated (w grows every iteration).

Other: objective is again the misfit about B0 (so the mean is one of the corrected modes); `psi_final.npy` saved per run.

## 8d. Slow build-up from no iron (2026-10-04)

- Initial design: no iron (ψ = const > 0). First step nucleates iron where the (range-compressed, `sens_power` = 0.5) sensitivity is most negative; scaling exponent < 1 keeps the ranking so iron appears where it is most effective.
- Step cap: per step, iron added + iron removed ≤ `mass_step_frac` × `mass_step_ref_kg` (1 % × 300 kg = 3 kg, full magnet). 1 % of the *current* mass is undefined from zero iron, hence the fixed reference (placeholder). Enforced without field solves: κ limited by bisection on the cut-ratio change (half the budget), Gauss–Newton shift scaled to the remainder.
- Coarse check (12 its): mean 124 → 158.4 mT in 10 steps at exactly 3 kg/step, then homogeneity starts falling; iron forms as pole pieces + back plates first.
- `scripts/run_fine.py`: maxh design/DSV/magnet/air = 22/12.5/20/100 mm → 49k tets, 263k dofs, ~15 s per forward solve (coarse ~3–5 s), ~1–2 min per iteration. Net growth ~2 kg/step (3 kg moved), so ≥ 100 iterations to place ~220 kg; estimate 4–6 h total, hard cap 250 iterations.

## 9. Open questions

1. DSV diameter (placeholder 200 mm)?
2. Pole magnet: grade, x/y/thickness, gap (placeholders N42, 300×300×50 mm, 300 mm)?
3. Iron $/kg (placeholder 2)?
4. Air-box size / far-field treatment acceptable (1 m octant)?
