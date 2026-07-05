"""Unit tests for the design_audit subprocess budget accounting."""

import build123d_mcp._design_audit_subprocess as subprocess_mod


def test_import_warmup_not_counted_in_baseline_cap(tmp_path, monkeypatch):
    class Clock:
        now = 100.0

        def monotonic(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    clock = Clock()
    caps = []
    warmed = {"done": False}

    def warm_import():
        clock.advance(10)
        warmed["done"] = True

    def evaluate_program(_program, cap_s):
        if not warmed["done"]:
            clock.advance(10)
            warmed["done"] = True
        caps.append(cap_s)
        clock.advance(1)
        return {
            "rebuilt": True,
            "passes_gate": True,
            "n_solids": 1,
            "volume": 100.0,
            "reasons": [],
        }

    monkeypatch.setattr(subprocess_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(subprocess_mod, "_warm_build123d_import", warm_import)
    monkeypatch.setattr(subprocess_mod, "evaluate_program", evaluate_program)

    subprocess_mod.run_audit(
        "t = 10.0\n",
        [{"name": "t", "value": 10.0, "type": "float"}],
        0.1,
        60,
        60,
        str(tmp_path / "audit.json"),
    )

    assert caps == [50, 8, 8]
