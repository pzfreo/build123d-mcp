"""Report installed versions of the server and its key dependencies.

Kept dependency-light (only importlib.metadata) so it stays fast, never
touches the OCC kernel, and works in-process — answering even when the
build123d worker subprocess is down (the stale-install case it diagnoses).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

# distribution names (as on PyPI); these double as the display labels
_PACKAGES = ("build123d-mcp", "build123d", "build123d-drafting-helpers")


def version_info() -> dict:
    """Return {distribution-name: version_string} for the server and its
    render-path deps, in display order.

    A package that is not installed reports "unknown" rather than raising,
    so the result is always a complete dict.
    """
    out: dict = {}
    for dist in _PACKAGES:
        try:
            out[dist] = _dist_version(dist)
        except PackageNotFoundError:
            out[dist] = "unknown"
    return out
