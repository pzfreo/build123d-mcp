"""design_audit() surfaces named parameters (Θ) and probes their robustness."""

import json

import pytest

from build123d_mcp.session import Session
from build123d_mcp.tools.design_audit import design_audit


@pytest.fixture
def session():
    s = Session()
    s.execute("from build123d import *")
    return s


def _run(session, program: str, **kwargs) -> dict:
    session.execute(program)
    return json.loads(design_audit(session, **kwargs))


def test_surfaces_named_parameters_with_types(session):
    r = _run(session, "t = 5.0\nn = 3\nshow(Box(t, t, t), 'p')\n")
    params = {p["name"]: p for p in r["parameters"]}
    assert params["t"] == {"name": "t", "value": 5.0, "type": "float"}
    assert params["n"] == {"name": "n", "value": 3, "type": "int"}


def test_robust_design_all_parameters_pass(session):
    r = _run(session, "w = 10.0\nh = 10.0\nd = 10.0\nshow(Box(w, h, d), 'r')\n")
    assert r["baseline"]["passes_gate"] is True
    assert r["summary"]["brittle"] == 0
    assert r["summary"]["robust"] == 3
    assert all(a["brittle"] is False for a in r["audit"])


def test_brittle_parameter_is_flagged(session):
    # A hollow cube: raising `inner` past `outer` over-subtracts to zero volume,
    # which fails the validity gate — a valid shape that is not a robust design.
    r = _run(
        session,
        "outer = 10.0\ninner = 9.5\nshow(Box(outer, outer, outer) - Box(inner, inner, inner), 'p')\n",
    )
    assert r["baseline"]["passes_gate"] is True
    audit = {a["name"]: a for a in r["audit"]}
    assert audit["inner"]["brittle"] is True
    assert r["summary"]["brittle"] >= 1
    # The +10% perturbation is the one that collapses it.
    failed = [p for p in audit["inner"]["perturbations"] if not p.get("passes_gate", True)]
    assert failed and any(p["delta_pct"] > 0 for p in failed)


def test_no_named_parameters_advises_hoisting(session):
    r = _run(session, "show(Box(10, 10, 10), 'x')\n")
    assert r["parameters"] == []
    assert r["inline_literal_count"] >= 3
    assert "parameter block" in r["note"]


def test_max_params_truncates_and_reports(session):
    r = _run(session, "a = 10.0\nb = 10.0\nc = 10.0\nshow(Box(a, b, c), 'r')\n", max_params=1)
    assert r["summary"]["audited"] == 1
    assert r["summary"]["truncated"] is True
    assert "Audited 1/3" in r["note"]


def test_empty_program_errors():
    s = Session()  # no execute() at all → no program to audit
    r = json.loads(design_audit(s))
    assert "error" in r


def test_invalid_epsilon_rejected(session):
    session.execute("w = 10.0\nshow(Box(w, w, w), 'r')\n")
    assert "error" in json.loads(design_audit(session, epsilon=1.5))
    assert "error" in json.loads(design_audit(session, epsilon=0))


def test_in_process_fallback_when_subprocess_blocked(session, monkeypatch):
    # On a host that blocks child processes, subprocess.run raises OSError and the
    # audit must still run (degraded, in-process) rather than erroring out.
    import build123d_mcp.tools.design_audit as da

    def _no_subprocess(*a, **k):
        raise OSError("child processes blocked")

    monkeypatch.setattr(da.subprocess, "run", _no_subprocess)
    r = _run(session, "w = 10.0\nshow(Box(w, w, w), 'r')\n")
    assert r["baseline"]["passes_gate"] is True
    assert "w" in [a["name"] for a in r["audit"]]


def test_security_config_propagated_to_subprocess(session, monkeypatch):
    # A program importing `os` (blocked by default, allowed via --allow-imports).
    # If the subprocess re-validated under the defaults instead of the live
    # session's config, the baseline would fail to rebuild.
    import build123d_mcp.security as sec

    monkeypatch.setattr(sec, "EXTRA_ALLOWED_IMPORTS", {"os"})
    r = _run(session, "import os\nw = 10.0\nshow(Box(w, w, w), 'r')\n")
    assert r["baseline"]["passes_gate"] is True
    assert "w" in [a["name"] for a in r["audit"]]


def test_reassigned_parameter_is_flagged_not_silently_passed(session):
    # A name assigned twice at the top level: perturbing the first assignment is
    # overwritten, so the audit must flag it as inconclusive, not report robust.
    r = _run(session, "t = 5.0\nt = 5.0\nshow(Box(t, t, t), 'p')\n")
    t_param = next(p for p in r["parameters"] if p["name"] == "t")
    assert t_param.get("reassigned") is True
    t_audit = next(a for a in r["audit"] if a["name"] == "t")
    assert "reassigned" in t_audit.get("note", "")


def test_baseline_that_fails_gate_is_reported(session):
    # A 2D sketch is not a solid — the baseline fails the gate, so robustness
    # auditing is premature and the tool says so rather than perturbing.
    r = _run(session, "size = 10.0\nshow(Rectangle(size, size), 'flat')\n")
    assert r["baseline"]["rebuilt"] is True
    assert r["baseline"]["passes_gate"] is False
    assert "audit" not in r
