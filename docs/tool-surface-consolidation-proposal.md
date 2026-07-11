# Proposal: Consolidate the default MCP tool surface

**Status:** Proposed
**Date:** 2026-07-11
**Related:** [#417](https://github.com/pzfreo/build123d-mcp/issues/417),
[PR #420](https://github.com/pzfreo/build123d-mcp/pull/420)

---

## Objective

Provide one normal MCP surface for both CAD generation and editing. The model should
see a small set of tools organised around user intent, while specialist and
experimental operations remain internal or require explicit CLI enablement.

The current default exposes approximately 38 tools. Several are implementation-level
variants of the same intent, increasing schema cost and tool-selection errors. This
proposal does not introduce generation, editing, or other workflow profiles. There is
one default product surface.

## Design principles

1. One default surface supports both generation and editing.
2. Tools are organised by workflow intent, not their underlying algorithm.
3. Specialist analysers remain separate, directly tested Python functions.
4. The default tools support complete workflows without CLI configuration.
5. Experimental tools remain hidden unless explicitly enabled.
6. A smaller surface must not become one tool with dozens of modes and parameters.
7. CLI switches may add specialist tools, but do not replace or alter the normal
   surface.

## Proposed default surface

### Modelling and artifacts

- `execute(code)`
- `import_cad_file(path, name)`
- `export(filename, format, object_name)`
- `render_view(...)`

### Inspection

- `inspect_part(object_name="", expected="", section_axis="Z", section_slices=7)`
- `compare(a="", b="", kind="shape")`
- `validate(object_name="")`

The standalone `measure` MCP tool remains visible during the migration. The normal
quick loop should use the existing in-`execute()` `measure(shape)` Python helper after
each boolean; `inspect_part` is the heavier checkpoint, not a replacement for every
fast measurement.

### Session

- `session_state()`
- `save_snapshot(name)`
- `restore_snapshot(name)`
- `script(save_to="")`
- `reset()`
- `last_error()`

### Drawing

The drawing tools remain in the default surface until #421 makes an explicit,
benchmark-backed decision about them:

- `inspect_drawing`;
- `view_axes`;
- `lint_drawing`;
- `render_drawing`;
- `save_drawing_annotations`;
- `suggest_view_layout`.

Drawing is a real supported workflow with its own skill. It must not be silently
dropped to meet an arbitrary tool-count target. Including it means the first
consolidated surface will likely contain approximately 18 to 20 tools rather than the
original 12-to-14 estimate.

`validate` remains separate because "is this a valid solid?" is a frequent operation
with a small, unambiguous contract. `compare` remains separate because before/after
comparison is central to editing and naturally operates on two objects.

## `inspect_part` contract

```text
inspect_part(
    object_name="",
    expected="",
    section_axis="Z",
    section_slices=7
)
```

The default response is compact and always includes:

- bounding envelope;
- solid and topology summary;
- grouped holes;
- grouped bosses;
- recognised patterns and member counts;
- cross-section variation;
- warnings;
- an optional expectation verdict.

Do not add `detail` modes in the initial consolidation. Compact default plus a
structured expectation language is already substantial; adding multiple diagnostic
modes risks turning `inspect_part` into the god tool this proposal explicitly rejects.
Follow-up evidence should first use the in-`execute()` analysis helpers. Automatic
principal-axis selection may later remove `section_axis` and `section_slices`, but only
after its reliability is demonstrated.

## Expectation contract

Keep the accepted schema narrow and geometry-focused:

```json
{
  "bbox": [100, 80, 20],
  "solid_count": 1,
  "holes": [
    {
      "count": 4,
      "diameter": 6,
      "axis": [0, 0, 1],
      "through": true
    }
  ],
  "bosses": [
    {
      "count": 2,
      "diameter": 12,
      "height": 8
    }
  ],
  "patterns": [
    {
      "type": "bolt_circle",
      "count": 1,
      "member_count": 4,
      "diameter": 50
    }
  ],
  "section_varying": true,
  "tolerance": 0.1
}
```

The report may return richer evidence such as centres, directions, counterbores,
spotfaces, and section samples. Those fields do not all need to become expectation
language. Unknown expectation fields must fail closed rather than silently weakening
the verdict.

## Consolidated specialist analysers

The following operations become internal analysers used by `inspect_part`:

- `cross_sections`;
- `find_holes`;
- `find_hole_patterns`;
- `find_bosses`;
- `find_bored_bosses`;
- `find_countersinks`;
- `locate_gate_defects`;
- the relevant implementation from `inspect_part`.

They should remain independently tested Python functions. Consolidation applies to
MCP exposure, not implementation structure.

`measure` is deliberately excluded from this initial removal list. Its standalone MCP
exposure can be reconsidered only after benchmark logs show that agents reliably use
the in-`execute()` helper for the tight boolean-verification loop.

`resolve` should become an `execute()` namespace helper unless usage evidence shows
that agents need it as a standalone MCP call.

Repair advice, workflow hints, quick references, cookbooks, and skill installation
operations should not occupy the normal modelling tool surface. Skills, resources,
and structured tool errors should carry that guidance.

## Specialist and experimental access

Retain the existing `--experimental` gate for unfinished tools such as `verify_spec`
and `suggest_spec`. Field evidence shows that these tools can reduce generation scores,
so the default workflow must not depend on them.

Add one explicit specialist switch:

```bash
build123d-mcp --specialist-tools
```

This adds low-level recognisers and diagnostic tools for maintainers and advanced
debugging. It is not a separate profile: all normal tools remain present and retain
the same contracts.

Granular compatibility enablement may be added if needed:

```bash
build123d-mcp --enable-tool find_holes
```

The existing opt-out `--disable-tool-groups` approach should eventually be replaced.
A small default with explicit additions is easier to understand than registering
everything and removing selected groups.

## Implementation plan

### 1. Inventory and classify tools

Assign every current MCP tool to one of: default, internal, specialist, experimental,
resource, or obsolete. Check skill references, tests, and generation/editing benchmark
logs before changing exposure.

### 2. Finalise `inspect_part`

Build on the issue-417 `inspect_part` checkpoint. Narrow its public arguments and
expectation schema. Add automatic section-axis selection and bounded, compact output.

### 3. Reuse internal analysers

Keep recognisers and measurement functions in their existing modules. Implement
`inspect_part` as orchestration over those functions rather than moving geometry logic
into one large module.

### 4. Prove editing coverage

Confirm that the compact report plus in-`execute()` helpers cover bored bosses,
countersinks, likely target holes, and other editing evidence. Add a new public
contract only when benchmark traces demonstrate a specific unresolved need.

### 5. Reduce default registration

First put the proposed registration changes behind a temporary development-only CLI
flag and A/B them against the unchanged default. This is migration scaffolding, not a
permanent user profile. Flip the normal registration default only after the benchmark
gate passes; then retain `--specialist-tools` as the explicit additive switch. Keep
worker methods and internal Python APIs intact.

### 6. Update skills and prompts

The normal generation workflow becomes:

```text
execute -> inspect_part -> render_view -> revise if needed -> inspect_part -> export
```

The normal editing workflow becomes:

```text
save_snapshot -> inspect_part -> execute edit -> compare -> inspect_part -> export
```

Neither workflow uses a special server profile.

### 7. Provide a compatibility window

Keep specialist exposure available through the CLI for existing integrations. Document
the migration and avoid maintaining duplicate default aliases indefinitely.

### 8. Benchmark before final removal

Compare the consolidated surface with the current default on both generation and
editing tasks. Measure score, valid completion rate, tool-call count, schema tokens,
retries, and incorrect tool selections.

## Acceptance criteria

- The default surface is materially smaller; its target count explicitly includes the
  retained drawing workflow rather than achieving a number by dropping capabilities.
- One default configuration supports both generation and editing.
- No benchmark-specific behaviour or expectations exist in MCP.
- `inspect_part` replaces routine standalone feature-recogniser and cross-section calls;
  the quick in-`execute()` measurement loop remains available.
- Specialist recognisers are available through explicit CLI opt-in.
- Experimental verification remains disabled by default.
- Generation and editing scores do not regress.
- Median analysis calls and tool-schema token cost decrease.
- Existing geometry analysers retain direct unit coverage.
- Large-shape inspection remains subprocess-bounded.

## Recommendation for PR #420

Ship PR #420 as the independently useful `inspect_part` checkpoint. Do not block it on
the broader surface migration tracked in #421.

Move the existing individual inspection tools behind specialist CLI enablement in a
follow-up PR, after benchmark comparison confirms that `inspect_part` covers editing as
well as generation.
