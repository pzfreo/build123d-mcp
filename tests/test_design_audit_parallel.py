"""design_audit's process pool, early-exit gate and budget handling (#490)."""

import concurrent.futures
import time

import pytest

import build123d_mcp._design_audit_subprocess as mod

PROGRAM = "from build123d import *\nw = 10.0\nh = 5.0\nresult = Box(w, h, h)\n"
PARAMS = [
    {"name": "w", "value": 10.0, "type": "float"},
    {"name": "h", "value": 5.0, "type": "float"},
]


def _audit(tmp_path, **kw):
    return mod.run_audit(PROGRAM, PARAMS, 0.1, 120, 60, str(tmp_path / "a.json"), **kw)


def _comparable(state):
    return [
        (
            p["name"],
            p["verdict"],
            [
                (e["delta_pct"], e["new_value"], e.get("passes_gate"), e.get("volume"))
                for e in p["perturbations"]
            ],
        )
        for p in state["audit"]
    ]


def test_pool_worthwhile_needs_real_savings():
    assert not mod._pool_worthwhile(10, 5.0, 1.0, workers=1)  # nothing to fan out to
    assert not mod._pool_worthwhile(1, 5.0, 1.0, workers=4)  # a single rebuild
    assert not mod._pool_worthwhile(6, 0.05, 1.0, workers=4)  # builds cheaper than startup
    assert mod._pool_worthwhile(6, 5.0, 1.0, workers=4)


def test_default_workers_is_capped_and_overridable(monkeypatch):
    monkeypatch.delenv("BUILD123D_AUDIT_WORKERS", raising=False)
    monkeypatch.setattr(mod.os, "cpu_count", lambda: 64)
    assert mod._default_workers() == mod._MAX_AUDIT_WORKERS
    monkeypatch.setenv("BUILD123D_AUDIT_WORKERS", "1")
    assert mod._default_workers() == 1
    monkeypatch.setenv("BUILD123D_AUDIT_WORKERS", "bogus")
    assert mod._default_workers() == mod._MAX_AUDIT_WORKERS


def test_pool_matches_sequential_results(tmp_path, monkeypatch):
    sequential = _audit(tmp_path, max_workers=1)
    monkeypatch.setattr(mod, "_pool_worthwhile", lambda *a, **k: True)
    pooled = _audit(tmp_path, max_workers=2)
    assert pooled["completed"] and sequential["completed"]
    assert _comparable(pooled) == _comparable(sequential)
    assert [p["name"] for p in pooled["audit"]] == ["w", "h"]  # input order kept


def test_pool_failure_is_logged_and_finishes_sequentially(tmp_path, monkeypatch, capsys):
    expected = _comparable(_audit(tmp_path, max_workers=1))

    class Broken:
        def __init__(self, *a, **k):
            raise OSError("no processes here")

    monkeypatch.setattr(mod, "_pool_worthwhile", lambda *a, **k: True)
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Broken)
    state = _audit(tmp_path, max_workers=2)
    assert _comparable(state) == expected
    assert "process pool failed" in capsys.readouterr().err


def test_stop_pool_kills_running_workers_without_waiting():
    executor = concurrent.futures.ProcessPoolExecutor(max_workers=2)
    for _ in range(4):
        executor.submit(time.sleep, 60)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and len(executor._processes) < 2:
        time.sleep(0.05)
    procs = list(executor._processes.values())
    manager = executor._executor_manager_thread
    time.sleep(0.5)  # let the workers pick up their tasks
    start = time.monotonic()
    mod._stop_pool(executor)
    for proc in procs:
        proc.join(timeout=10)
    assert time.monotonic() - start < 10
    assert not any(proc.is_alive() for proc in procs)
    # The audit subprocess must be able to exit: a live non-daemon manager thread
    # would hold interpreter shutdown until the parent hard-kills it.
    manager.join(timeout=10)
    assert not manager.is_alive()


def test_early_exit_gate_agrees_with_full_gate():
    from build123d import Box, Shell

    from build123d_mcp.tools.validate import _gate_report

    box = Box(10, 10, 10)
    assert _gate_report(box, early_exit=True) == _gate_report(box, early_exit=False)

    open_shell = Shell(list(box.faces())[:-1])  # one face missing: not watertight
    fast = _gate_report(open_shell, early_exit=True)
    full = _gate_report(open_shell, early_exit=False)
    assert fast["passes_gate"] is False and full["passes_gate"] is False
    assert fast["mesh_check"] == "skipped"
    assert set(fast["reasons"]) <= set(full["reasons"])


def test_genuine_type_errors_are_not_retried(tmp_path, monkeypatch):
    calls = []

    def boom(program, cap_s, early_exit=True):
        calls.append(early_exit)
        raise TypeError("real bug in the build")

    monkeypatch.setattr(mod, "evaluate_program", boom)
    with pytest.raises(TypeError, match="real bug"):
        _audit(tmp_path, max_workers=1)
    assert calls == [False]  # the baseline ran once; no silent second attempt
