import json

from build123d_mcp.session import Session
from build123d_mcp.tools.execute import execute_code
from build123d_mcp.tools.export import bank_candidate


def _session_with_box() -> Session:
    session = Session()
    session.execute("from build123d import *")
    execute_code(session, "show(Box(10, 10, 10), 'part')")
    return session


def test_bank_candidate_promotes_and_snapshots_only_after_gate_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = _session_with_box()

    result = json.loads(
        bank_candidate(session, "output.step", object_name="part", snapshot_name="floor")
    )

    assert result["banked"] is True
    assert result["snapshot_saved"] is True
    assert "floor" in session.snapshots
    assert (tmp_path / "output.step").exists()
    assert "inspect_features" in result["next_tool_call"]
    assert not list(tmp_path.glob("bank-candidate-*"))


def test_bank_candidate_preserves_existing_output_and_skips_snapshot_on_failure(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    session = _session_with_box()
    output = tmp_path / "output.step"
    output.write_bytes(b"known-good")

    import build123d_mcp.tools.validate as validate_module

    monkeypatch.setattr(
        validate_module,
        "_gate_report",
        lambda shape, exact=False, mesh_override=None: {
            "passes_gate": False,
            "reasons": ["injected mesh-open edge"],
            "mesh_check": "exact-subprocess",
            "overlapping_pairs": 0,
            "overlap_check": "exact",
        },
    )
    result = json.loads(
        bank_candidate(session, "output.step", object_name="part", snapshot_name="bad")
    )

    assert result["banked"] is False
    assert result["existing_output_preserved"] is True
    assert result["snapshot_saved"] is False
    assert "bad" not in session.snapshots
    assert output.read_bytes() == b"known-good"
    assert "repair_advice" in " ".join(result["next_tool_calls"])
    assert not list(tmp_path.glob("bank-candidate-*"))
