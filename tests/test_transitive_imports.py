"""Tests for the transitive-safe import checker.

A pure-Python package whose entire import closure lies within the security
allowlist must be importable without --allow-imports.  Any package that
directly or indirectly imports a blocked module (os, subprocess, socket …)
must still be blocked.
"""

import pytest

import build123d_mcp.security as _sec
from build123d_mcp.security import _clear_transitive_cache, _is_transitively_safe
from build123d_mcp.session import Session

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cache():
    """Guarantee a clean cache before and after every test."""
    _clear_transitive_cache()
    yield
    _clear_transitive_cache()


def _make_pkg(tmp_path, name: str, files: dict[str, str], monkeypatch) -> str:
    """Create a package under tmp_path and prepend it to sys.path."""
    pkg = tmp_path / name
    pkg.mkdir()
    for rel, src in files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


# ---------------------------------------------------------------------------
# _is_transitively_safe — unit tests
# ---------------------------------------------------------------------------


def test_allowlist_root_is_safe():
    assert _is_transitively_safe("math") is True


def test_allowlist_submodule_is_safe():
    assert _is_transitively_safe("collections.abc") is True


def test_nonexistent_module_is_blocked():
    assert _is_transitively_safe("no_such_module_xyz_abc_999") is False


def test_draftwright_is_allowlisted():
    """draftwright (the AGPL drawing engine spun out of build123d-drafting-helpers,
    #270) is a deliberate bring-your-own dependency: not installed by this Apache
    server, but permitted in the sandbox so users who install it can
    `from draftwright import make_drawing` without --allow-imports. The membership
    check must short-circuit before the transitive find_spec, so the import passes
    even when draftwright is absent."""
    assert _sec._is_root_allowed("draftwright") is True
    # check_ast validates the name against the allowlist without importing it,
    # so this does not require draftwright to be installed.
    _sec.check_ast("from draftwright import make_drawing")


def test_safe_package_allowed(tmp_path, monkeypatch):
    pkg = _make_pkg(
        tmp_path,
        "mypkg",
        {
            "__init__.py": "from mypkg.utils import helper\n",
            "utils.py": "import math\nimport collections\n\ndef helper(): pass\n",
        },
        monkeypatch,
    )
    assert _is_transitively_safe(pkg) is True


def test_safe_submodule_allowed(tmp_path, monkeypatch):
    _make_pkg(
        tmp_path,
        "mypkg2",
        {
            "__init__.py": "",
            "core.py": "import math\n",
        },
        monkeypatch,
    )
    assert _is_transitively_safe("mypkg2.core") is True


def test_package_importing_os_is_blocked(tmp_path, monkeypatch):
    pkg = _make_pkg(
        tmp_path,
        "badpkg",
        {
            "__init__.py": "import os\n",
        },
        monkeypatch,
    )
    assert _is_transitively_safe(pkg) is False


def test_transitive_unsafe_dep_is_blocked(tmp_path, monkeypatch):
    """A package that looks safe on top but imports os via a helper is blocked."""
    pkg = _make_pkg(
        tmp_path,
        "indirectpkg",
        {
            "__init__.py": "from indirectpkg.helper import thing\n",
            "helper.py": "import os\n\ndef thing(): pass\n",
        },
        monkeypatch,
    )
    assert _is_transitively_safe(pkg) is False


def test_cyclic_safe_package_allowed(tmp_path, monkeypatch):
    """Two modules that import each other, both using only allowed deps."""
    _make_pkg(
        tmp_path,
        "cyclicpkg",
        {
            "__init__.py": "",
            "a.py": "from cyclicpkg.b import b_fn\nimport math\n\ndef a_fn(): pass\n",
            "b.py": "from cyclicpkg.a import a_fn\nimport math\n\ndef b_fn(): pass\n",
        },
        monkeypatch,
    )
    assert _is_transitively_safe("cyclicpkg.a") is True
    assert _is_transitively_safe("cyclicpkg.b") is True


def test_result_is_cached(tmp_path, monkeypatch):
    pkg = _make_pkg(
        tmp_path,
        "cachepkg",
        {
            "__init__.py": "import math\n",
        },
        monkeypatch,
    )
    _is_transitively_safe(pkg)
    assert pkg in _sec._transitive_safe_cache


def test_relative_imports_within_package_are_followed_and_safe(tmp_path, monkeypatch):
    """Relative imports are followed; a package that only uses allowed deps remains safe."""
    _make_pkg(
        tmp_path,
        "relpkg",
        {
            "__init__.py": "",
            "sub.py": "from . import utils\n",
            "utils.py": "import math\n",
        },
        monkeypatch,
    )
    assert _is_transitively_safe("relpkg.sub") is True


def test_empty_package_is_safe(tmp_path, monkeypatch):
    pkg = _make_pkg(
        tmp_path,
        "emptypkg",
        {
            "__init__.py": "",
        },
        monkeypatch,
    )
    assert _is_transitively_safe(pkg) is True


def test_package_importing_blocked_ocp_submodule_is_unsafe(tmp_path, monkeypatch):
    """A package that imports a blocked OCP sub-module (OCP.OSD) must not be transitively safe."""
    pkg = _make_pkg(
        tmp_path,
        "ocppkg",
        {
            "__init__.py": "import OCP.OSD\n",
        },
        monkeypatch,
    )
    assert _is_transitively_safe(pkg) is False


def test_type_checking_guard_not_blocked(tmp_path, monkeypatch):
    """Imports inside 'if TYPE_CHECKING:' are not executed at runtime; package must be allowed."""
    pkg = _make_pkg(
        tmp_path,
        "typechecking_pkg",
        {
            "__init__.py": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    import os  # never executes at runtime\n"
                "import math\n"
            ),
        },
        monkeypatch,
    )
    assert _is_transitively_safe(pkg) is True


def test_relative_import_to_blocked_submodule_is_unsafe(tmp_path, monkeypatch):
    """'from . import evil_utils' where evil_utils.py imports os must be blocked."""
    _make_pkg(
        tmp_path,
        "relimport_evil",
        {
            "__init__.py": "from . import evil_utils\n",
            "evil_utils.py": "import os\n",
        },
        monkeypatch,
    )
    assert _is_transitively_safe("relimport_evil") is False


def test_parent_init_with_blocked_import_blocks_submodule(tmp_path, monkeypatch):
    """'from mypkg.utils import X' must be blocked if mypkg/__init__.py imports os."""
    _make_pkg(
        tmp_path,
        "bad_parent",
        {
            "__init__.py": "import os\n",
            "utils.py": "import math\n",
        },
        monkeypatch,
    )
    assert _is_transitively_safe("bad_parent.utils") is False


# ---------------------------------------------------------------------------
# Integration: Session.execute() with transitive packages
# ---------------------------------------------------------------------------


def test_execute_safe_package_allowed(tmp_path, monkeypatch):
    _make_pkg(
        tmp_path,
        "goodpkg",
        {
            "__init__.py": "from goodpkg.core import helper\n",
            "core.py": "import math\n\ndef helper(): return math.pi\n",
        },
        monkeypatch,
    )
    s = Session()
    result = s.execute("import goodpkg")
    assert "not allowed" not in result.lower()
    assert "Error" not in result


def test_execute_safe_submodule_from_import(tmp_path, monkeypatch):
    _make_pkg(
        tmp_path,
        "goodpkg2",
        {
            "__init__.py": "",
            "geom.py": "import math\n\ndef area(r): return math.pi * r * r\n",
        },
        monkeypatch,
    )
    s = Session()
    result = s.execute("from goodpkg2.geom import area; x = area(5)")
    assert "not allowed" not in result.lower()
    assert "Error" not in result


def test_execute_unsafe_package_blocked(tmp_path, monkeypatch):
    _make_pkg(
        tmp_path,
        "evilpkg",
        {
            "__init__.py": "import subprocess\n",
        },
        monkeypatch,
    )
    s = Session()
    result = s.execute("import evilpkg")
    assert "not allowed" in result.lower()


def test_execute_transitively_unsafe_blocked(tmp_path, monkeypatch):
    _make_pkg(
        tmp_path,
        "sneakypkg",
        {
            "__init__.py": "from sneakypkg.loader import load\n",
            "loader.py": "import os\n\ndef load(): return os.listdir('.')\n",
        },
        monkeypatch,
    )
    s = Session()
    result = s.execute("import sneakypkg")
    assert "not allowed" in result.lower()


def test_execute_hint_mentions_import_cad_file(tmp_path, monkeypatch):
    """The error message for a blocked import should surface import_cad_file."""
    _make_pkg(
        tmp_path,
        "blocked_hint_pkg",
        {
            "__init__.py": "import os\n",
        },
        monkeypatch,
    )
    s = Session()
    result = s.execute("import blocked_hint_pkg")
    assert "import_cad_file" in result
