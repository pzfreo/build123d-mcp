# What build123d-mcp can learn from Arko-T (text-to-design)

**Source:** Wang, Xi, Xiang et al., *"Arko-T: Text-to-Design Generation for Parametric CAD"*,
arXiv:2606.30429v1, 29 Jun 2026. Arko-T is a 4B-parameter model that maps a natural-language
prompt to an executable **build123d** program. We are not that model — but the paper's framing of
*what a good CAD generation is* maps directly onto gaps in this server.

This note records the analysis. Its headline recommendation shipped as the `design_audit()` tool (#330).

---

## The core idea: a shape to render vs. a design to edit

The paper's thesis (abstract, §1, §3) is that text-to-CAD systems optimize for the wrong target.
They chase **executability** — does the program run, and does it produce a valid solid? — when the
thing an engineer actually needs is a preserved **design state**:

> z = (F, Θ, C, H, A)

| Symbol | Meaning | Example |
|--------|---------|---------|
| **F** | Feature vocabulary | holes, ribs, fillets, shells, patterns |
| **Θ** | Named parameters (adjustable) | `hole_radius`, `plate_thickness`, `vent_slot_spacing` |
| **C** | Constraints / relations | symmetry, coplanarity, spacing rules |
| **H** | Construction history | the ordered sketch → extrude → cut → fillet sequence |
| **A** | Attachments / references | a feature bound to a specific face, edge, or sketch plane |

The paper's slogan: *"a shape to render, not a design to edit."* A syntactically valid script can
produce an empty body, a non-manifold solid, or a shape unrelated to the request — and even a
geometrically perfect shape is useless as a *design* if you can't change the hole spacing without
regenerating from scratch (§3, "Distinction from code generation").

### The mirror this holds up to build123d-mcp

The server is **excellent on the executability axis and nearly empty on the design-state axis.**

- `tools/validate.py` (~980 lines) is the paper's **validity gate** (the IR / "invalid rate"
  metric, §5.1) — and stricter than the paper's: BRepCheck well-formedness, edge-face watertight/
  manifold counts, *mesh-level* non-manifold edges and vertices, and an open-edge deflection ladder.
  Its own docstring references CADGenBench.
- `tools/shape_compare.py` is the **CD / volumetric-IoU** analog (§5.1) — surface-deviation regions
  plus exact added/removed volume.
- `tools/find_features.py` (`find_holes`, `find_hole_patterns`, `find_bosses`) recognizes the **F**
  axis.
- `measure(min_wall_thickness)` senses the thin-wall failure mode (§5.5).

But every one of those tools operates on the **executed geometry `g`**, never on the **design `z`**
that produced it. The server can prove you made a valid *shape*; it cannot tell you whether you made
an editable *design*. That blind spot is exactly the half of the problem the paper exists to argue
for.

---

## Recommendations, ranked

### 1. Design-state audit / parametric-robustness tool — *implemented as `design_audit()` (#330)*

> **Status: shipped.** This recommendation is now the `design_audit()` tool — it surfaces the
> program's top-level numeric parameters and perturbs each ±ε, re-running the validity gate to flag
> *brittle* parameters (a small edit that collapses the solid). The rebuild+gate loop runs in a
> hard-bounded subprocess (see [ADR 0002](adr/0002-worker-subprocess-crash-containment.md)). The rest
> of this section is the original design rationale.

This is the paper's own headline future-work item (§6):

> *"Developing feature-level evaluation — automated checking of whether each requested feature is
> realized and whether the design remains valid after parameter edits."*

A grep of `src/` confirms the server has **no** parameter-surfacing or parameter-perturbation
capability (the only `params` concept is `library.load_part`'s `make(**params)`, for pre-authored
library parts, not the live session program). A new tool would:

- **Surface Θ** — parse the current program's top-level numeric assignments (the named-parameter
  block) and report them. The model cannot reason about editability if it cannot see the knobs.
- **Perturb and re-validate** — re-execute the program with each named parameter nudged by ±ε
  (e.g. ±10%) and run the existing `_gate_report` from `validate.py` on each result. A parameter
  that collapses the solid under a 10% change reveals a brittle "design" — precisely the thin-wall /
  coordinate-reasoning failure mode the paper documents (§5.5), caught *structurally* rather than by
  luck.

This converts "valid shape" into "valid, editable design" reusing machinery that already exists.
It is the single change most faithful to the paper. **Delivered in #330.**

### 2. Push design-state code structure in the guidance — *delivered alongside #330*

§4.3 ("Design-State Code Normalization") is a recipe adoptable almost verbatim for the guidance the
server gives the *calling* model:

- **Named parameter block at the top, with units** (`plate_width = 100.0  # mm`) instead of inline
  magic constants.
- **Canonical feature idioms** so feature names map to construction patterns.
- **Consistent construction order** (sketch → extrude → secondary features → finishing).
- **Explicit constraints / references** rather than implicitly computed coordinates.

> **Status: shipped (guidance).** An "Author for editability" section is now in the b123d-modeling
> skill (Step 2) and in `default_prompt.md`, and `quickref.py` gained a runnable **Pattern 3:
> design-state authoring** exemplar (named parameter block + units, base → secondary → finishing
> order). Previously the quickref taught the *opposite* — nearly every snippet was `Box(20, 10, 5)`
> with inline literals. Still open as an optional extra: a `validate_code.py` advisory when a program
> has many inline numeric literals and no parameter block.

### 3. Tie feature detection to *intent* (feature-realization check)

The detection primitives exist (`find_holes`, `find_hole_patterns`, `find_bosses` — the **F** axis).
What is missing is comparing *detected* features against *requested* ones. §5.5 notes models "score
well while omitting a rib or a bolt pattern" because CD/IoU mask missing features. A lightweight
"feature manifest" assertion — *"expected 4 holes in a bolt circle; found 3"* — closes that gap and
composes with the existing recognizers.

### 4. Failure-mode-targeted lint

§5.5 names the recurring failure modes exactly: revolves/sweeps along complex paths (coordinate
reasoning), **thin-walled constructions where small numerical errors collapse geometry**, and
**polar patterns where the model must infer the array axis and count**. The server already has the
sensor for the worst one (`measure(min_wall_thickness)`) but it is opt-in and manual. Auto-flagging
sub-threshold walls, degenerate revolve profiles (the quickref already *warns* about a profile
crossing the axis — promote that to a check), and ambiguous pattern axes targets the documented
frontier.

### 5. The server as the oracle for a self-improving loop — *strategic / optional*

§6: *"generated programs that pass kernel validation become new training data, compounding the
model's coverage over successive rounds."* build123d-mcp is exactly the execution-grounding oracle
that loop needs — it already executes, validates, measures, and renders build123d programs. An
optional structured log of `(prompt, program, validity-gate verdict, measurements)` would turn the
server into a data generator for the kind of training the paper describes. A product-direction note
more than a code change.

---

## What the paper validates about the current design

- The **hard validity gate before scoring** (§5.1: "must execute *and* produce a non-empty, valid
  solid") is `validate.py` — and this server's version is stricter (mesh-level non-manifold
  edges/vertices, open-edge deflection ladder). Keep it.
- **Geometric agreement metrics** (CD / IoU) map to `shape_compare.py`. One paper-aligned
  refinement: its docstring notes it compares A-vs-B, *not* against a reference answer — the paper
  always scores against ground truth, so a "compare to reference STEP" mode would make it a true
  scorer.
- **build123d as the backend** — the paper chose build123d for its 1.3M-program training corpus,
  third-party validation of this project's bet.

---

## Bottom line

The server had nailed the executability half of the paper and a blind spot on the design-state
half — the exact half the paper exists to argue for. The highest-value, most paper-faithful move,
**#1, a design-state / parametric-robustness audit tool** built on the `_gate_report` machinery in
`validate.py`, has now shipped as `design_audit()` (#330), and its companion #2 (design-state
authoring guidance) shipped with it in the skill, `default_prompt.md`, and `quickref.py`.
Recommendations #3–#5 (feature-realization check, failure-mode lint, oracle/self-improving loop)
remain open — they are new features, not doc changes.
