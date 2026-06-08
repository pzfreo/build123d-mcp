#!/usr/bin/env python3
"""Rewrite server.json's version (and package versions) to a given version.

Keeps the MCP registry manifest aligned with the package version. Used by the
release workflow (the registry-publish step syncs to the release tag; the
bump-version step syncs to the next dev base) and guarded by
``tests/test_server_json_version.py``. Operates on ``server.json`` in the current
working directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def sync_server_json(version: str, path: Path = Path("server.json")) -> None:
    """Set the top-level and per-package ``version`` fields in server.json."""
    data = json.loads(path.read_text())
    data["version"] = version
    for package in data.get("packages", []):
        package["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    sync_server_json(sys.argv[1])
