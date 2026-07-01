"""design_audit — surface a program's named numeric parameters (Θ) and probe
their robustness by perturbing each ±ε and re-running the existing validity gate.

This is the Arko-T §6 "design to edit, not a shape to render" idea: every other
tool inspects the executed geometry g; this one inspects the *design* z that
produced it. A parameter whose small change collapses the solid (or drops it
below the validity gate) reveals a brittle design — the thin-wall / coordinate
failure mode the paper documents (§5.5), caught structurally rather than by luck.

Isolation & budget: the audit reads only ``execute_history`` (the live session is
never mutated). The rebuild+gate loop runs in a **hard-bounded subprocess** —
because a rebuild or the gate's mesh tessellation can enter an un-interruptible
native OCC call that SIGALRM cannot stop — which the parent kills on overrun
without touching the worker. The subprocess persists results incrementally, so a
kill still yields a salvaged partial report. On hosts that block child processes
the loop runs in-process (SIGALRM-bounded, degraded), mirroring locate/compare.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

from build123d_mcp._design_audit_subprocess import _extract_params, run_audit
from build123d_mcp.tools._budget import op_budget

# Parent reserves this under the op budget for teardown + partial-result salvage +
# response formatting; the subprocess gets a shorter soft budget so it can finish
# and mark itself complete before the parent's hard kill fires. The grace covers
# interpreter + build123d startup (the subprocess's internal deadline clock only
# starts after that) plus the final flush and teardown.
_AUDIT_MARGIN_S = 15
_AUDIT_SUBPROC_GRACE_S = 10


def _security_config() -> dict:
    """Snapshot the worker's live import/sandbox globals to hand to the subprocess."""
    import build123d_mcp.security as sec

    return {
        "allow_all_imports": bool(sec.ALLOW_ALL_IMPORTS),
        "no_sandbox": bool(sec.DISABLE_SANDBOX),
        "extra_allowed_imports": sorted(sec.EXTRA_ALLOWED_IMPORTS),
    }


def _assemble(session) -> str:
    blocks = list(getattr(session, "execute_history", []))
    if not blocks:
        return ""
    first = blocks[0]
    if "from build123d import" not in first and "import build123d" not in first:
        blocks = ["from build123d import *"] + blocks
    return "\n\n".join(blocks)


def _hoist_note(inline_literals: int, n_params: int) -> str:
    return (
        f" {inline_literals} inline numeric literals remain outside the parameter block — "
        "hoist the load-bearing ones to named parameters for full editability (Arko-T §4.3)."
        if inline_literals > n_params
        else ""
    )


def design_audit(session, epsilon: float = 0.1, max_params: int = 8) -> str:
    """Surface the current program's named parameters and audit their robustness.

    For each top-level numeric parameter, rebuild the program with the parameter
    nudged ±epsilon and run the validity gate on the result. A parameter is
    brittle if any perturbation fails to rebuild or fails the gate.

    Known limitation: only literal-valued top-level assignments are surfaced as
    parameters. A derived parameter (e.g. ``radius = diameter / 2``) is not listed
    on its own — perturb the upstream literal it depends on instead.
    """
    if not 0 < epsilon < 1:
        return json.dumps({"error": "epsilon must be between 0 and 1 (e.g. 0.1 for ±10%)."})
    if max_params < 1:
        return json.dumps({"error": "max_params must be >= 1."})

    program = _assemble(session)
    if not program:
        return json.dumps(
            {"error": "No executed program to audit. Run execute() to build geometry first."}
        )

    try:
        params, inline_literals = _extract_params(program)
    except SyntaxError as exc:
        return json.dumps({"error": f"Could not parse the assembled program: {exc}"})

    if not params:
        return json.dumps(
            {
                "parameters": [],
                "inline_literal_count": inline_literals,
                "note": (
                    "No top-level numeric parameters found. This program uses inline literals "
                    "instead of a named parameter block, so it is a shape to render, not a design "
                    "to edit (Arko-T §4.3). Hoist key dimensions to named top-level assignments "
                    "with units, e.g. `plate_thickness = 5.0  # mm`, then re-run design_audit."
                ),
            },
            indent=2,
        )

    audited = params[:max_params]
    budget = op_budget(session)
    parent_timeout = max(_AUDIT_MARGIN_S, budget - _AUDIT_MARGIN_S)
    budget_s = max(5, parent_timeout - _AUDIT_SUBPROC_GRACE_S)
    cfg = {
        "program": program,
        "params": audited,
        "epsilon": epsilon,
        "budget_s": budget_s,
        # Per-rebuild ceiling scales with the op budget so a heavy-but-valid build
        # (which needs a raised --exec-timeout) isn't falsely failed by a fixed cap;
        # the total is still bounded by remaining() and the parent hard-kill.
        "per_run_cap": budget,
        # Propagate the live session's import/sandbox config so the subprocess
        # validates rebuilds exactly as the worker would (see #143 flags).
        "security": _security_config(),
    }

    work = tempfile.mkdtemp(prefix="b123d_audit_")
    in_json = os.path.join(work, "in.json")
    out_json = os.path.join(work, "out.json")
    salvaged = False
    proc = None
    try:
        with open(in_json, "w") as f:
            json.dump(cfg, f)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "build123d_mcp._design_audit_subprocess", in_json, out_json],
                capture_output=True,
                text=True,
                timeout=parent_timeout,
            )
        except subprocess.TimeoutExpired:
            salvaged = True  # read whatever the subprocess persisted before the kill
        except OSError:
            # Host blocks child-process creation (#143 / InProcessSession): no
            # subprocess and no worker op-timeout, so run in-process (degraded).
            run_audit(program, audited, epsilon, budget_s, budget, out_json)

        state = _read_state(out_json)
        if state is None or state.get("baseline") is None:
            detail = (
                (proc.stderr or "")[-200:] if proc else "no result before the time budget elapsed"
            )
            return json.dumps({"error": f"design audit produced no result: {detail}"})

        return _format(state, params, audited, inline_literals, epsilon, salvaged)
    finally:
        # rmtree (not per-file unlink) so a leftover atomic-write .tmp from a
        # killed subprocess doesn't strand the temp dir.
        shutil.rmtree(work, ignore_errors=True)


def _read_state(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _format(state, params, audited, inline_literals, epsilon, salvaged) -> str:
    baseline = state["baseline"]
    if not baseline.get("rebuilt"):
        return json.dumps(
            {
                "parameters": params,
                "baseline": {"rebuilt": False, "error": baseline.get("error")},
                "note": "The program does not rebuild in isolation, so robustness cannot be audited. "
                "Make the session program self-contained (see script()), then re-run.",
            },
            indent=2,
        )
    if not baseline.get("passes_gate"):
        return json.dumps(
            {
                "parameters": params,
                "baseline": {
                    "rebuilt": True,
                    **{k: baseline[k] for k in ("passes_gate", "n_solids", "volume", "reasons")},
                },
                "note": "The baseline design does not pass the validity gate. Fix it (see validate()) "
                "before auditing parameter robustness.",
            },
            indent=2,
        )

    audit = state.get("audit", [])
    n_audited = len(audit)
    completed = bool(state.get("completed"))
    truncated = len(params) > len(audited) or n_audited < len(audited) or not completed or salvaged
    # Each audited param falls in exactly one bucket: inconclusive (reassigned, a
    # no-op rebuild), else brittle, else robust — so robust+brittle+inconclusive
    # == audited and `robust` never over-counts an unprobed param.
    inconclusive_count = sum(1 for a in audit if a.get("inconclusive"))
    brittle_count = sum(1 for a in audit if a.get("brittle") and not a.get("inconclusive"))

    note = (
        "For editing, verify each brittle parameter: a small change should not collapse a robust "
        "design. Perturbation is structural (rebuild + validity gate), not a score."
    )
    note += _hoist_note(inline_literals, len(params))
    if truncated:
        note += (
            f" Audited {n_audited}/{len(params)} parameters"
            + (" (time budget exhausted)" if (not completed or salvaged) else " (max_params cap)")
            + " — raise max_params or --exec-timeout to cover the rest."
        )

    return json.dumps(
        {
            "parameters": params,
            "baseline": {
                "rebuilt": True,
                **{k: baseline[k] for k in ("passes_gate", "n_solids", "volume")},
            },
            "inline_literal_count": inline_literals,
            "audit": audit,
            "summary": {
                "total_params": len(params),
                "audited": n_audited,
                "robust": n_audited - brittle_count - inconclusive_count,
                "brittle": brittle_count,
                "inconclusive": inconclusive_count,
                "truncated": truncated,
                "epsilon": epsilon,
            },
            "note": note,
        },
        indent=2,
    )
