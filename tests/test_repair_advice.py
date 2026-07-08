import json
from pathlib import Path

from build123d_mcp.tools.repair_advice import repair_advice

ROOT = Path(__file__).resolve().parents[1]


def test_repair_advice_matches_split_bored_boss_extension():
    out = json.loads(
        repair_advice(
            error_text=(
                "Validity gate: FAIL - B-rep is not well-formed (BRepCheck failed); "
                "4 open edge(s) - not watertight; exported output.step still invalid"
            ),
            goal="Increase the length of the square boss with rounded corners and a central bore by 10mm.",
            context="The face extrusion created a separate new bore segment.",
        )
    )

    assert out["kind"] == "repair_advice"
    assert "split_bored_boss_extension" in out["matched_recipe_ids"]
    assert "open_shell_or_disjoint_edit_rebuild" in out["matched_recipe_ids"]

    recipes = {r["id"]: r for r in out["recipes"]}
    boss = recipes["split_bored_boss_extension"]
    assert any("re-cut the central bore" in step for step in boss["approach"])
    assert any("fills the bore" in stop for stop in boss["stop_conditions"])
    assert "execute()" in out["note"]


def test_repair_advice_defaults_to_generic_gate_sequence():
    out = json.loads(repair_advice("unexpected CAD failure"))

    assert out["matched_recipe_ids"][0] == "gate_first_baseline"
    assert "malformed_face_local_repair" in out["matched_recipe_ids"]
    assert "open_shell_or_disjoint_edit_rebuild" in out["matched_recipe_ids"]
    assert out["markdown"].startswith("Repair advice")


def test_workflow_skills_mention_repair_advice():
    skills_dir = ROOT / "src" / "build123d_mcp" / "skills"
    for skill in ("b123d-edit", "b123d-modeling", "b123d-repair"):
        text = (skills_dir / skill / "SKILL.md").read_text()
        assert "repair_advice()" in text
