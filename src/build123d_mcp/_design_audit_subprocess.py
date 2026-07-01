"""Out-of-process design-state audit for ``design_audit`` (see tools/design_audit.py).

Run as ``python -m build123d_mcp._design_audit_subprocess <in.json> <out.json>``.
``in.json`` = ``{program, params, epsilon, budget_s, per_run_cap}``. Rebuilds the
program once per perturbed parameter in a fresh ``Session`` and runs the validity
gate on each result, writing to ``out.json`` **incrementally** (after the baseline
and after every parameter) so the parent can salvage a partial report if it
hard-kills this child at the op-budget deadline.

Why a subprocess: a rebuild — or the gate's mesh tessellation — can enter an
un-interruptible native OCC call (boolean / fillet / ``BRepMesh``) that no
in-worker SIGALRM can stop. Running the whole audit here lets the parent bound it
with ``subprocess.run(timeout=)`` and kill this disposable child on overrun,
without ever SIGKILLing the worker and destroying the session (issue #307). The
pure-AST helpers (parameter extraction / perturbation) also live here so the tool
module can import them without a heavy or circular import.
"""

import ast
import json
import os
import sys
import time

# Stop starting new rebuilds when this little of the soft budget remains.
_STOP_THRESHOLD_S = 2
# Cap each perturbation rebuild at this multiple of the measured baseline rebuild
# time (floored) so one pathological/slow rebuild can't consume the whole budget
# and starve later params — while a heavy-but-valid build (perturbation ≈ baseline)
# still fits comfortably.
_BASELINE_CAP_FACTOR = 4
_MIN_PERTURB_S = 8


def _const_number(node):
    """Return the numeric value of a constant / signed-constant node, else None.

    bool is a subclass of int but is not a design parameter, so it is excluded.
    """
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.USub, ast.UAdd))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        v = node.operand.value
        return -v if isinstance(node.op, ast.USub) else v
    return None


def _assign_target(node):
    """Return (name, value_node) for a single-Name top-level assignment, else None."""
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id, node.value
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    ):
        return node.target.id, node.value
    return None


def _extract_params(program: str):
    """Surface Θ: top-level ``name = <number>`` assignments.

    Returns ``(params, inline_literal_count)``. A name assigned more than once at
    the top level is marked ``reassigned`` — perturbing its first assignment would
    be silently overwritten, so the audit can flag it rather than falsely pass it.
    inline_literal_count is all numeric literals minus the surfaced parameters — a
    rough "magic constant" signal for the authoring-style advisory.
    """
    tree = ast.parse(program)
    total_numeric = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, (int, float))
        and not isinstance(n.value, bool)
    )
    top_names: dict[str, int] = {}
    for node in tree.body:
        target = _assign_target(node)
        if target is not None:
            top_names[target[0]] = top_names.get(target[0], 0) + 1
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            # An augmented reassignment (t += 1) also overwrites a perturbation.
            top_names[node.target.id] = top_names.get(node.target.id, 0) + 1

    params: list[dict] = []
    seen: set[str] = set()
    for node in tree.body:
        target = _assign_target(node)
        if target is None:
            continue
        name, valnode = target
        if name in seen:
            continue
        num = _const_number(valnode)
        if num is None:
            continue
        seen.add(name)
        entry = {"name": name, "value": num, "type": "int" if isinstance(num, int) else "float"}
        if top_names[name] > 1:
            entry["reassigned"] = True
        params.append(entry)
    return params, max(0, total_numeric - len(params))


def _rewrite(program: str, name: str, new_value) -> str:
    """Rewrite the first top-level assignment of ``name`` to ``new_value``, re-emit source."""
    tree = ast.parse(program)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = _assign_target(node)
        if target is not None and target[0] == name:
            node.value = ast.Constant(value=new_value)
            break
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _perturbations(value, is_int: bool, eps: float):
    """Return perturbation dicts for ±eps: ``{delta_pct, new_value, discrete}``.

    ``delta_pct`` is the *realized* change (from ``new_value`` vs ``value``), so a
    discrete integer bump like 4→5 reports 25, not the nominal 10. ``discrete`` is
    True when ±eps rounded back to the original int and it was stepped by ±1
    instead. Directions that don't change the value (zero-valued float) are
    dropped, and a discrete step that would drive a positive int to ≤ 0 is dropped
    — that is a "remove the feature" edit, not a ±eps robustness probe.
    """
    out = []
    for sign in (1, -1):
        discrete = False
        nv = value * (1 + sign * eps)
        if is_int:
            nv = int(round(nv))
            if nv == value:
                nv = value + sign  # ±eps rounded to no change → discrete ±1 step
                discrete = True
            if value > 0 and nv <= 0:
                continue  # positive count/dimension to ≤0 is a removal, not a nudge
        else:
            nv = round(nv, 6)
        if nv == value:
            continue  # zero-valued float: ±eps is a no-op — unperturbable
        delta_pct = round((nv - value) / value * 100) if value else None
        out.append({"delta_pct": delta_pct, "new_value": nv, "discrete": discrete})
    return out


def evaluate_program(program: str, cap_s: int) -> dict:
    """Rebuild ``program`` in a fresh Session and gate the result.

    Returns ``{rebuilt: False, error}`` or
    ``{rebuilt: True, passes_gate, n_solids, volume, reasons}``.
    """
    from build123d_mcp.session import Session
    from build123d_mcp.tools.validate import _gate_report

    sess = Session(exec_timeout=max(3, int(cap_s)))
    result = sess.execute(program)
    if result.startswith("Error:") or result.startswith("Constraint failed"):
        return {"rebuilt": False, "error": result[:200]}
    if sess.current_shape is None:
        return {"rebuilt": False, "error": "program ran but produced no shape/solid"}
    try:
        report = _gate_report(sess.current_shape)
    except Exception as exc:  # a shape the gate can't analyse is not robust
        return {
            "rebuilt": True,
            "passes_gate": False,
            "n_solids": None,
            "volume": None,
            "reasons": [f"gate error: {exc}"[:200]],
        }
    return {
        "rebuilt": True,
        "passes_gate": report["passes_gate"],
        "n_solids": report["n_solids"],
        "volume": round(report["volume"], 4),
        "reasons": report["reasons"],
    }


def run_audit(
    program: str, params: list, epsilon: float, budget_s: float, per_run_cap: int, out_path: str
) -> dict:
    """Baseline + per-parameter perturbation audit, persisted incrementally.

    Writes ``out_path`` after the baseline and after each parameter so a hard kill
    still leaves a salvageable partial report. Returns the final state dict.
    """
    deadline = time.monotonic() + budget_s

    def remaining() -> float:
        return deadline - time.monotonic()

    def cap(limit: float) -> int:
        return max(3, min(int(limit), int(remaining())))

    state: dict = {"baseline": None, "baseline_ok": False, "audit": [], "completed": False}

    def flush() -> None:
        # Atomic: write a temp file then os.replace, so a hard kill mid-write
        # leaves the previous complete snapshot intact for the parent to salvage.
        tmp = out_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, out_path)

    # Baseline gets the full per-run budget (it may be the heaviest single build);
    # time it so perturbations can be capped relative to it.
    t_base = time.monotonic()
    state["baseline"] = evaluate_program(program, cap(per_run_cap))
    base_time = time.monotonic() - t_base
    state["baseline_ok"] = bool(
        state["baseline"].get("rebuilt") and state["baseline"].get("passes_gate")
    )
    flush()
    if not state["baseline_ok"]:
        state["completed"] = True
        flush()
        return state

    base_vol = state["baseline"]["volume"]
    perturb_limit = max(_MIN_PERTURB_S, min(per_run_cap, int(base_time * _BASELINE_CAP_FACTOR) + 2))

    for p in params:
        if remaining() <= _STOP_THRESHOLD_S:
            break
        if p.get("reassigned"):
            # The later top-level assignment overwrites the one we rewrite, so the
            # rebuild is a guaranteed no-op — skip it (reclaim budget) and mark the
            # parameter inconclusive rather than counting it as robust.
            state["audit"].append(
                {
                    **p,
                    "perturbations": [],
                    "brittle": False,
                    "inconclusive": True,
                    "note": "reassigned at top level — perturbing the first assignment is overwritten; audit inconclusive",
                }
            )
            flush()
            continue
        results: list = []
        brittle = False
        for pert in _perturbations(p["value"], p["type"] == "int", epsilon):
            if remaining() <= _STOP_THRESHOLD_S:
                break
            g = evaluate_program(
                _rewrite(program, p["name"], pert["new_value"]), cap(perturb_limit)
            )
            entry = {"delta_pct": pert["delta_pct"], "new_value": pert["new_value"]}
            if pert["discrete"]:
                entry["discrete_step"] = True
            if not g.get("rebuilt"):
                brittle = True
                entry["rebuilt"] = False
                entry["error"] = g.get("error")
                results.append(entry)
                continue
            entry["rebuilt"] = True
            entry["passes_gate"] = g["passes_gate"]
            entry["n_solids"] = g["n_solids"]
            entry["volume"] = g["volume"]
            if base_vol:
                entry["volume_delta_pct"] = round((g["volume"] - base_vol) / base_vol * 100, 1)
            if not g["passes_gate"]:
                brittle = True
                entry["reasons"] = g["reasons"]
            results.append(entry)
        state["audit"].append({**p, "perturbations": results, "brittle": brittle})
        flush()

    # completed only if every parameter was audited — a soft-budget break leaves
    # it False so the parent labels the partial report "time budget exhausted".
    state["completed"] = len(state["audit"]) >= len(params)
    flush()
    return state


def _apply_security(sec: dict) -> None:
    """Mirror the live session's import/sandbox config (worker sets these from CLI
    flags at bootstrap; a fresh subprocess would otherwise re-validate under the
    defaults and falsely reject programs that rely on --allow-imports/--no-sandbox)."""
    import build123d_mcp.security as _sec

    if sec.get("no_sandbox"):
        _sec.DISABLE_SANDBOX = True
    if sec.get("allow_all_imports"):
        _sec.ALLOW_ALL_IMPORTS = True
    if sec.get("extra_allowed_imports"):
        _sec.EXTRA_ALLOWED_IMPORTS.update(sec["extra_allowed_imports"])


def main(in_json: str, out_json: str) -> None:
    with open(in_json) as f:
        cfg = json.load(f)
    _apply_security(cfg.get("security") or {})
    run_audit(
        cfg["program"], cfg["params"], cfg["epsilon"], cfg["budget_s"], cfg["per_run_cap"], out_json
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
