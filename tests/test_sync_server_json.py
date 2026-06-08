"""scripts/sync_server_json.py rewrites server.json versions consistently."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_server_json.py"


def test_sync_rewrites_top_level_and_package_versions(tmp_path: Path) -> None:
    server = tmp_path / "server.json"
    server.write_text(json.dumps({"version": "0.0.1", "packages": [{"version": "0.0.1"}]}))

    subprocess.run([sys.executable, str(_SCRIPT), "9.9.9"], cwd=tmp_path, check=True)

    data = json.loads(server.read_text())
    assert data["version"] == "9.9.9"
    assert all(pkg["version"] == "9.9.9" for pkg in data["packages"])
    assert server.read_text().endswith("\n")  # consistent trailing newline
