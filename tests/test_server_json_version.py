"""server.json registry metadata must track the package version (issue #181)."""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _package_base_version() -> str:
    """The pyproject version with any ``.devN`` suffix stripped."""
    text = (_ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, "could not find version in pyproject.toml"
    return re.sub(r"\.dev\d+$", "", match.group(1))


def test_server_json_version_matches_package() -> None:
    base = _package_base_version()
    server = json.loads((_ROOT / "server.json").read_text())

    assert server["version"] == base, (
        f"server.json version {server['version']!r} != package base {base!r}"
    )
    for package in server.get("packages", []):
        assert package["version"] == base, (
            f"server.json package version {package['version']!r} != package base {base!r}"
        )
