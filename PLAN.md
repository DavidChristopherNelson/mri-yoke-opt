# Plan (revised 2026-09-27; objective and design variables superseded by Plan X, section 10, 2026-10-05)

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

## 10. Plan X (2026-10-05): free iron + ferrite topology, cost per good imaging voxel

Sections 1–8 describe the earlier formulation (fixed NdFeB pole magnets, DSV sphere, field misfit + ppm). What is kept from it: the HCurl A-formulation with Brauer iron and damped Newton, CG + BDDC, the level-set / cut-ratio machinery, the line search with a Gauss–Newton trust region, exports, timing, session runner, checkpoints. What changed:

### 10.1 Design variables

- No fixed magnet. Design domain = 1/8 design box minus the patient/bed keep-out envelope `[0, env_x/2] × [0, env_y/2] × [0, env_z/2]` (placeholders 400 × 400 × 300 mm full size).
- Two level sets on the same mesh: ψ < 0 iron, ψ_f < 0 ferrite; ferrite wins overlaps, so cr + cr_f ≤ 1.
- Per-element magnetization direction m_e (unit vector; the brief's p ∈ {+1, −1} along z was generalised on 2026-10-06).
- Material law (geometric mix): ν = ν0^(1−cr−cr_f) · ν_iron(B)^cr · (ν0/μr_f)^cr_f. Source everywhere in the design domain: M = cr_f · (Br_f/μ0)/μr_f · m_e. Ferrite: Br_f = 0.40 T, μr_f = 1.05, 4900 kg/m³.
- Access corridor (2026-10-06): the envelope's x–z footprint extruded along y through the design box holds no material (`corridor`), so the patient can enter along y.
- Sensitivities for any functional (K λ = ∂J/∂A): dJ/dcr_e = −∫_e ∂ν/∂cr curlA·curlλ; dJ/dcr_f,e = −∫_e ∂ν/∂cr_f curlA·curlλ + M_f m_e·∫_e curlλ. Checked against finite differences (`scripts/test_gradient.py`).
- Direction: dJ/dm_e = cr_f,e M_f ∫_e curlλ, so the direction that lowers J is m_e = −∫_e curlλ_J / |…| (λ_J the adjoint of the minimised J). Elements without ferrite take it outright (new ferrite nucleates with its best direction); elements with ferrite turn towards it by at most `dir_rot_max` (0.2 rad, halved after a failed line search) per step, inside the line search, so the field change stays controlled. Noise seeds give every ferrite blob one random direction (uniform on the sphere, from the seed's generator).
- Step caps per accepted iteration: iron moved (added + removed) ≤ `mass_step_frac_fe` × `iron_ref_kg`, ferrite moved ≤ `mass_step_frac_f` × `ferrite_ref_kg` (5 % × 300 kg = 15 kg, 5 % × 200 kg = 10 kg since 2026-10-05, 1 % before; reference masses are placeholders). Each level set gets its own cap on κ, so a tight cap on one material does not slow the other; the mean-field correction is scaled to respect both.
- Demagnetisation gate: per ferrite element h = H·(p e_z) = (ν0/μr_f) p B_z − M_f; reported `demag_frac` = ferrite volume fraction with h < −0.8 Hcj(T_cold) (250 kA/m default); quadratic penalty `demag_weight` × Σ cr_f vol ((−0.8 Hcj − h)/Hcj)²₊ / V_ref added to the objective.
- Cost in dollars for the full magnet: C = C_fe + C_f + C_fixed (C_fixed $2000; placeholders: iron $2/kg, ferrite $3/kg).

### 10.2 Objective

minimise F = (C_fe + C_f + C_fixed) / N_green

- Imaging voxels: regular grid, edge `dx_img` = 3 mm, filling the envelope box (1/8 in the model).
- A voxel is green when, at its centre, | |B| − B_c | ≤ dB_band/2 (B_c = 0.1592 T, fixed) and | ∂|B|/∂r | ≤ s_max = κ_s · dB_band / (2 δ dx_img), r = readout axis (y).
- Hardware derivation: dB_band = min(ism_fraction × 0.70 mT, 2 δ dx_img G_max), ism_fraction = 0.5, δ = 5 (field-map corrected; 1 if not), G_max = 10 mT/m, κ_s = 0.4. With the defaults: dB_band = 0.30 mT (G_max binds; the ISM limit would be 0.35 mT), s_max = 4 mT/m. Logged at the start of every run.
- Exact count (reported): |B| from the FE solution at the voxel centres, slope by central differences on the voxel grid, both tests, 6-connected components with the symmetry planes as mirrors; N_green = largest component, counted in the full magnet (a component touching t symmetry planes joins 2^t of its mirror images).
- Smooth count (differentiated): N_s = Σ_x σ((dB_band/2 − |b − B_c|)/w_b) · σ((s_max − |∂b/∂r|)/w_s), b = Σ_k a_k φ_k the projection of |B| on the even harmonic polynomials up to degree `harm_order` (8 → 15 modes). ∂N_s/∂cr = Σ_k (∂N_s/∂a_k)(∂a_k/∂cr), one adjoint solve per mode, one Jacobian. No connectivity in the gradient.
- Widths: geometric continuation from `anneal_start` × (dB_band, s_max) to `anneal_end` × (…) over `anneal_iters` iterations (1 → 0.05 over 100).
- The optimiser minimises J = ln(C / N_s) + demagnetisation penalty (same minimiser as C/N_s; the logarithm stays finite while N_s is astronomically small).
- Gauss–Newton correction only for the mean field of the current largest green component (projection mean over its voxels; sphere of radius 50 mm at the origin while there is none), target B_c.
- Removed: field misfit, ppm, mean tolerance, penalty weight w, `constraints_ok`, DSV, clearance shell, magnet body.

### 10.3 Precondition (SNR)

`scripts/precheck.py`: K0 √T_scan / √(1 + γ t_ov dB_band / (4π δ)) ≥ SNR* (1 + κ_s), with G_r = dB_band / (2 δ dx_img). The optimiser never uses SNR. **K0 is not defined in the brief**; the script uses K0 = ω0 B1⁻ M0 dx³ f_seq / (F_rx √(4 k_B (T_coil R_coil + T_body R_body))), M0 = ρ γ² ħ² B_c / (4 k_B T_body), f_seq = (1 − e^(−TR/T1)) κ_T2. Replace if a different definition was meant.

### 10.4 Seeds and multi-start

- `init=noise:<seed>`: two white-noise fields on a grid of pitch `h_seed` (10 mm), Gaussian-blurred with correlation length `ell_seed` (40 mm), thresholded at the quantiles that give 20 % iron and 10 % ferrite of the design volume; each connected ferrite blob gets one random direction. Deterministic given the seed; no randomness inside the loop.
- `init=hframe:thin|medium|thick`: iron back plate + post, ferrite slab 30 / 50 / 80 mm at the old magnet position.
- `scripts/multistart.py`: N noise seeds + 3 H-frame seeds through `session.py`, then `summary.csv` and IoU matrices of the final iron and ferrite masks.
- With 20 % iron a noise seed starts at ~1000 kg; it sheds mass slowly even at the 5 % cap (15 kg per step; ~700 iterations to the band at 1 %).

### 10.5 Deviations from the Plan X brief and things found while building it

| brief | built | why |
|---|---|---|
| projection Gram matrix over the whole envelope | over a sphere of radius `proj_radius` (100 mm) at the centre, meshed as its own region; the smooth count sums the voxels inside it. `proj_radius=0` gives the brief's variant | sources touch the envelope box, so the polynomial projection does not converge there: on the H-frame start |B| spans 9–324 mT in the box, residual 7 mT rms and a 5 mT bias at the centre, unchanged for degree 6–12. The band is ±0.15 mT. In the sphere the residual is the FE noise of the mesh |
| widths start at ~dB_band, ~s_max | same schedule, but w_b ≥ rms deviation of the projected field from B_c over the blob (w_s widened by the same factor) | 80 mT from the band a 0.3 mT sigmoid sees one voxel; J was then not a usable function of the design (line search failed at iteration 2) |
| polarity ±z, updated every iteration | free unit vector per element; new ferrite takes the best direction, existing ferrite turns ≤ 0.2 rad per step (10.1) | user request 2026-10-06; a jump of existing ferrite is outside the line search |
| one κ limited by both caps | κ capped per level set | the iron cap throttled ferrite growth to a third of its cap |
| K0 given | assumed (10.3) | not in the brief |
| exact count needs |B| and ∂|B|/∂r "from the FE solution" | slope by central differences of the sampled |B| | curl A is piecewise linear and discontinuous; no usable pointwise derivative |

Found on the way:
- The exact cut-ratio formula of the earlier code cancelled catastrophically when a tet had several identical nodal values (signed-distance level sets have flat parts): elements flipped between 0 and 1 for a 1e-9 perturbation. Replaced by closed forms per number of negative vertices without such differences.
- The H-frame start is now a signed-distance level set; the ±1 indicator lost 6 % of the slab volume to interpolation.
- The Newton reference residual is recomputed per solve (the source now depends on the design).

### 10.6 Validation

| check | result |
|---|---|
| Stage 1 forward: H-frame + slab with Br_f = 1.3 T vs old coarse run, iteration 0 | 227.0 mT vs 227.5 mT (DSV-surface mean) |
| Stage 1 adjoint vs finite differences (misfit, demag; cr, cr_f, polarity source) | within 2 % (demag on empty elements 4 %) |
| Stage 1 optimisation, 8 iterations | runs; field rises 0.6 mT/iteration under the caps |
| Stage 2 adjoint vs finite differences (ln N_s, demag; cr, cr_f, polarity source), δ = 1e-2 | ln N_s within 1.5 %; demag within 8 % (kinked penalty) |
| Stage 2 optimisation, H-frame medium, 6 iterations | blob mean 71 → 78 mT, ferrite +1.35 kg/iteration (cap 2), iron cap (3 kg) binding |

Coarse mesh: FE noise in |B| is ~1–3 mT in the projection sphere against a ±0.15 mT band, so N_green on this mesh says nothing about the design; coarse runs validate the pipeline only. `resid=` in the log is that noise; a warning is printed once it exceeds dB_band/4 inside a green blob.

### 10.7 Open questions (Plan X)

1. Envelope size (placeholder 400 × 400 × 300 mm)?
2. Costs: ferrite $/kg, iron $/kg? (C_fixed = $2000 set 2026-10-05)
3. Reference masses for the step caps (300 kg iron, 200 kg ferrite)?
4. K0 definition for the precheck?
5. Projection sphere radius (100 mm) acceptable, or mesh the envelope so finely near the sources that a box projection is not needed?
6. Corridor: is the full envelope footprint (400 × 300 mm) the right access cross-section?

### 10.8 Multi-start run planx_ms2 (2026-10-05/06, ±z polarity, no corridor, 5 % caps, C_fixed $2000; stopped before batch 3 ended)

11 coarse runs, ~100 iterations each. All 8 noise seeds (start ~500 kg ferrite) reached 159.2 mT within 50–70 iterations; no H-frame seed did in ~100 (slab grew 1–2 kg/step). Best: noise7 F = $10.9/voxel (N_green 440), noise2 $13.4 (368); the other seeds ended with 1–2 green voxels. N_green on this mesh is FE noise against the ±0.15 mT band (`resid` 0.06–0.2 mT); N_smooth ~4000 for the good runs. Shapes: two large +z ferrite blocks above/below the envelope, iron behind, no closed return yoke; IoU between seeds 0.1–0.3. Results (without viewers / VTK) in `results/planx_ms2`.
