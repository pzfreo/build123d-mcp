# Proposal: Design Conformance — an intent-to-solid verification gate

**Status:** MVP shipped — tracked in [#335](https://github.com/pzfreo/build123d-mcp/issues/335). See *Implementation (MVP)* below.
**Author:** design partner review, 2026-07-01
**Inspired by:** Arko-T (arXiv:2606.30429, "a design to edit, not a shape to render") and
`armpro24-blip/cad-cae-copilot` (the aieng workbench). Builds on `design_audit()` (#330) and
[`arko-t-design-state-analysis.md`](arko-t-design-state-analysis.md).

---

## The one thing

Add a **declared design-intent spec** and a **`verify_spec()` conformance gate** that checks
the built solid against *what was actually requested* — feature-by-feature, parameter-by-parameter —
and returns a single **evidence-tiered conformance report** that states what is *proven*, what is
*unverified*, and never says "the design is correct."

That's it. One capability: **close the loop between intent and geometry, honestly.**

Today build123d-mcp can prove a solid is *valid* (`validate`), locate *where* it's wrong
(`locate_gate_defects`), measure it (`measure`), recognise its features (`find_holes` /
`find_hole_patterns` / `find_bosses`), and check its *parameters* are robust (`design_audit`). What it
**cannot** do is answer the question the user actually asked: *"did I build the thing that was
requested?"* A part can pass every existing check and still be missing a rib, have the bolt circle at
the wrong PCD, or violate the envelope — the model "scores well while omitting a feature" (Arko-T
§5.5). Conformance is the missing gate.

## Why this is the right one thing

**It is the synthesis of the three sources, each contributing its best idea:**

- **Arko-T** gives the framing: a design is `z = (F, Θ, C, H, A)` — features, parameters,
  constraints, history, attachments. `design_audit` already covers **Θ** (robustness). A spec makes
  **F** (features) and **C** (constraints) *first-class and checkable*, and directly implements the
  paper's own recommendation #3 (feature-realization evaluation) — the piece we deferred. It turns
  intent from something implicit in a prompt into a versioned, machine-checkable artifact.

- **cad-cae-copilot** gives the discipline we lack: **evidence/claim honesty**. Its strongest,
  most novel idea is the refusal to let "it executed" mean "it's valid," and its **credibility
  tiers** (executed > proxy > surrogate > critique). We adopt exactly that: every conformance line
  carries the *tier of evidence* behind it, and the report never advances an unearned claim.

- **build123d-mcp** gives the moat that makes it *real*: a genuine B-rep validity gate + kernel
  measurement + feature recognition. cad-cae-copilot *has* an evidence framework but **no B-rep
  validity gate** — so its "conformance" can only ever be artifact-level. We can check conformance
  against actual geometry. This is the one place we are deeper than they are, and the spec gate is
  where that depth pays off.

**It fits our identity.** It is a *gate*, not a workbench. No CAE, no UI, no agent-driving, no new
heavy dependency — those are cad-cae-copilot's bets and out of scope for us (see Non-goals). It is
one or two sharp tools built entirely on machinery we already ship.

## What it looks like

### 1. The spec — a small declarative intent contract

The agent (or user) declares intent as checkable predicates, not prose. Example:

```yaml
# design.spec.yaml  (or passed inline as JSON)
envelope_mm:   { x: [0, 100], y: [0, 60], z: [0, 20] }   # C: bounding constraint
solid:         { count: 1, valid: true }                   # C: one watertight/manifold body
features:                                                   # F: requested features
  - kind: hole_pattern
    pattern: bolt_circle
    holes: 4
    bcd_mm: 40
    diameter_mm: 6.6
  - kind: boss
    diameter_mm: 12
    height_mm: 8
min_wall_mm:   2.0                                          # C: manufacturability constraint
parameters:                                                 # Θ: expected knobs (feeds design_audit)
  - { name: plate_thickness, min: 4, max: 8 }
targets:                                                    # things we CANNOT verify here
  - { name: fatigue_life_cycles, min: 1.0e6, verifiable: false }
```

Kept deliberately small and geometry-centric. Every field maps to an existing query.

### 2. `verify_spec(spec, object_name="")` — the gate

Checks each requirement against the current shape using tools we already have, and classifies each
result by **evidence tier**:

| Tier | Meaning | Source |
|---|---|---|
| `measured` | Read directly from the kernel — highest confidence | `measure`, bounding box, volume, `clearance` |
| `structural` | Passes the B-rep/mesh validity gate | `validate` / `_gate_report` |
| `recognised` | Found by heuristic feature recognition — good, not infallible | `find_holes` / `find_hole_patterns` / `find_bosses` |
| `robust` | Survives ±ε parameter perturbation | `design_audit` |
| `asserted` | A design-rule `require()` the author declared | user assertion, unchecked geometry |
| `unverified` | Requested but **we have no tool to prove it** (e.g. fatigue, thermal) | declared `verifiable: false` |

### 3. The report — honest by construction

```jsonc
{
  "conformance": [
    {"requirement": "envelope x∈[0,100]", "status": "PASS", "tier": "measured", "actual": 98.2},
    {"requirement": "1 valid solid",       "status": "PASS", "tier": "structural"},
    {"requirement": "4× Ø6.6 bolt circle Ø40",
     "status": "FAIL", "tier": "recognised", "found": "3 holes on Ø40 BCD",
     "hint": "one hole missing or off-pattern — see find_hole_patterns"},
    {"requirement": "min wall ≥ 2.0mm", "status": "PASS", "tier": "measured", "actual": 2.4},
    {"requirement": "plate_thickness robust ±10%", "status": "PASS", "tier": "robust"},
    {"requirement": "fatigue_life ≥ 1e6", "status": "UNVERIFIED", "tier": "unverified",
     "note": "no solver in build123d-mcp; declared unverifiable — do not claim as met"}
  ],
  "summary": {"pass": 4, "fail": 1, "unverified": 1, "conforms": false},
  "note": "verify_spec proves REQUESTED-vs-BUILT for the geometry-checkable requirements only. "
          "UNVERIFIED requirements are NOT met — they are out of scope for this gate. This is not a "
          "certification; a human must sign off."
}
```

The `note` and the per-line tiers are the cad-cae-copilot discipline in action: the tool structurally
cannot report "design is correct," only "these specific requirements are proven, by this evidence,
and these are not."

## Why it's more than the sum of the existing tools

- **It's a contract, so it's reusable.** A spec authored once becomes a **regression/acceptance
  gate**: re-run `verify_spec` after any `execute()` edit and collateral breakage (a boolean that
  silently dropped a hole, an edit that pierced a wall) is caught immediately — this is
  cad-cae-copilot's "regression diff on every edit," achieved with our validity/recognition depth.
- **It closes the CADGenBench blind spot.** Geometric-agreement scores (CD/IoU) mask missing
  features; a feature-realization gate catches "3 of 4 holes" that a volume delta would hide.
- **It makes intent inspectable.** The spec *is* the design's `F`+`C`; combined with `design_audit`
  (Θ), `script()` (H), and `resolve` refs (A), the full Arko-T `z` becomes first-class in a session.

## Scope

**MVP (one PR):** the spec schema (envelope, solid count/validity, hole-pattern & boss features,
min-wall, parameter ranges) + `verify_spec()` + the tiered report, built on existing tools. Ship
`verify_spec` reading an inline JSON spec or a `.spec.yaml` path.

**Later (separate, optional):** (a) `suggest_spec()` — draft a spec from the current shape so the
agent can confirm/edit intent rather than write it from scratch; (b) fold the conformance verdict +
spec + `script()` + STEP + validity report into a single **reproducible design bundle** (our answer
to the `.aieng` package — but geometry-correctness-first); (c) auto-repair loop
(`verify_spec` FAIL → `locate_gate_defects` / feature diff → targeted `execute` → re-verify).

## Implementation (MVP — shipped)

`verify_spec(spec="", spec_path="", object_name="")` in `tools/verify_spec.py`, composing existing
checkers only (no new geometry code, no subprocess, no new dependency; `_GEOMETRY_TIMEOUT` op).

**Decisions made for the MVP:**
1. **`min_wall_mm` — deferred.** It has no clean kernel query today (`measure` doesn't expose it), so
   rather than route it through `analyze_printability` now, a spec that requests it returns
   **UNVERIFIED** (honest, not silently ignored). Revisit as a later PR.
2. **Spec input — both.** Inline JSON (`spec=`) *and* a `.json` file path (`spec_path=`, via the
   existing `safe_output_path` policy).
3. **`conforms` — excludes UNVERIFIED.** `conforms = (fail == 0)`; UNVERIFIED requirements never
   count as passing *or* as failing — they are explicitly out of scope for the gate.

**Shipped checks:** `envelope_mm`, `solid {count, valid}`, `volume_mm3`, features
(`hole_pattern`/`hole`/`boss`), `parameters` (top-level numeric range). Dimensions match within
`max(0.1 mm, 1%)`; counts exact; an unrecognised feature `kind` → UNVERIFIED (never a false FAIL).
Tiers emitted: `measured`, `structural`, `recognised`, `unverified`.

**Still deferred (later PRs):** the `robust` tier (wire in `design_audit`), `min_wall_mm`, YAML specs,
`suggest_spec()`, the reproducible design bundle, and the verify→repair→re-verify loop.

## Non-goals (deliberate restraint)

- **No CAE/FEA.** `targets` that need a solver are declared `unverifiable` and reported UNVERIFIED —
  we do not pretend. (That is cad-cae-copilot's domain and a different bet.)
- **No UI, no agent-driving, no new heavy dependency.** Stays a focused MCP server.
- **Not a certification.** The report says so, explicitly, every time.
- **Not a DSL.** The spec is a thin declarative map onto existing queries, not a new modelling language.

## Risks / open questions

- **Feature-recognition is heuristic** (`recognised` tier is honest about this) — a spec requiring an
  unrecognised feature type returns UNVERIFIED, not a false FAIL. The tier system contains this.
- **Spec authoring burden** — mitigated by keeping the schema small and by the later `suggest_spec()`.
- **Matching semantics** — how strict is "4× Ø6.6 on Ø40"? Needs sensible tolerances (reuse the
  callout-matching logic already in `find_hole_patterns` / drawing lint).

## Success metric

On the editing/spec-driven CADGenBench fixtures: **a measurable drop in "passes validity gate but
omits/mis-locates a requested feature"** — the exact failure mode Arko-T §5.5 names and that none of
our current gates catch. Secondary: agents stop over-claiming ("the part is correct") because the
tool only ever hands them tiered, bounded evidence.

---

**One line:** turn build123d-mcp from *"here is a valid shape"* into *"here is exactly which of your
requested requirements are proven, by what evidence, and which are not"* — Arko-T's design-state made
checkable, with cad-cae-copilot's evidence honesty, on top of the B-rep validity depth only we have.
