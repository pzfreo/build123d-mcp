# Single-fixture eval harness

Run one drawing → solid fixture through **Claude Code + the gate-equipped
build123d-mcp server**, with a live event stream, and score the result. Built to
smoke-test changes (especially the `validate()` gate) on a real fixture before
committing to an expensive full CADGenBench sweep.

## Prerequisites

- `claude` (Claude Code) and `uvx` on `PATH`.
- This repo (for `score.py`, which imports `build123d_mcp.tools.validate`).
- The run uses `build123d-mcp@latest` from PyPI by default — make sure the
  release you want to test is published (the `validate()` gate landed in
  **0.3.51**). Pass a pinned spec as the 4th arg to `run_fixture.sh` otherwise.

## 1. Get a fixture

CADGenBench inputs are public (ground truth is held out). Generation fixtures
are `101–150`, editing `201+`:

```
uv run --with huggingface_hub python eval/fetch_fixture.py 102 /tmp/cgb_102_in
```

That writes `input.png` (+ `description.yaml`, and `input.step` for editing) into
`/tmp/cgb_102_in`. (Or point at any directory that contains an `input.png`.)

NIST CTC/FTC also works as input (it has ground truth), but its drawings are
**model-based-definition** sheets that omit most nominal dimensions, so scale is
not recoverable from them — good for validity/pipeline tests, poor for accuracy.

## 2. Run (with live logging)

```
eval/run_fixture.sh /tmp/cgb_102_in /tmp/cgb_102_run            # Opus 4.8 by default
```

In a second terminal, watch it live:

```
tail -n0 -f /tmp/cgb_102_run/stream.jsonl | python3 eval/stream_filter.py /tmp/cgb_102_run
```

The stream surfaces tool calls, `validate()`/`export` results, errors, and the
agent's reasoning as they happen — so a stuck validate-fix loop is visible
immediately. The full timeline is also written to
`/tmp/cgb_102_run/filtered.log`. Result lands at `/tmp/cgb_102_run/output.step`.

## 3. Score

```
uv run --project . --with trimesh --with scipy \
    python eval/score.py /tmp/cgb_102_run/output.step
```

Reports the **validity gate** (PASS/FAIL + reasons). Add a ground-truth STEP to
also get an indicative **shape score** (scale+align search → surface-distance
F1 + Chamfer):

```
... python eval/score.py /tmp/cgb_102_run/output.step /path/to/ground_truth.step
```

## Notes / gotchas learned the hard way

- **Validity vs timeout.** The `validate()` → fix loop helps validity but costs
  wall-clock; a long run can time out (= a zero, same as invalid). The prompt is
  deadline-aware ("export once valid + major features match; don't over-polish").
  Watch the live stream for a loop that won't converge.
- **CADGenBench drawings are dimensioned** (scale recoverable). **NIST MBD
  drawings are not** (dimensions live in the model).
- **PMI pollution:** imported CAD STEP carries annotation curves that inflate
  bounding boxes / edge counts. Always work from `.solids()` — `score.py` does.
- The shape score here is an indicative proxy, **not** the official CADGenBench
  metric. Use it for relative comparison, not leaderboard-equivalent numbers.
