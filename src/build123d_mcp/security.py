"""
Lightweight defence-in-depth for exec'd user code.

Two layers applied before exec() is called:
  1. AST inspection  — rejects dangerous imports and calls.
  2. Restricted builtins — namespace __builtins__ has open/eval/exec removed
     and __import__ filtered to the allowlist.

Timeout is enforced by the caller via SIGALRM (Session) or by killing the
worker process (WorkerSession).

This is not a complete sandbox. The AST check blocks the most common
subclass-traversal escape paths (dunder attribute access, getattr/vars/dir)
but ctypes, C extensions, and build123d internals are not further restricted.
The goal is to raise the bar against realistic prompt-injection payloads.
"""

import ast
import importlib.util
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EXEC_TIMEOUT_SECONDS = 120

# Modules user code may import. build123d's own internal imports are
# unaffected — they run through the real import system, not this namespace.
IMPORT_ALLOWLIST = frozenset({
    # CAD libraries
    "build123d",
    "bd_warehouse",
    "build123d_drafting",
    # Numeric / math
    "math",
    "numpy",
    "decimal",
    "fractions",
    "statistics",
    "numbers",
    "random",
    # Data structures / utilities
    "collections",
    "itertools",
    "functools",
    "copy",
    "operator",
    "struct",
    # Type system
    "typing",
    "abc",
    "dataclasses",
    "enum",
    # String / text
    "re",
    "string",
    "textwrap",
    "pprint",
    # Serialisation (in-memory only — no I/O)
    "json",
    "base64",
    "hashlib",
    # Misc stdlib
    "io",
    "warnings",
    "contextlib",
    # Introspection — signature(), getdoc(), getmembers() are read-only and help with
    # API discovery without requiring docs. Cannot execute code.
    "inspect",
})

# OCP (OpenCASCADE Python bindings) sub-modules that are safe to import.
# These are purely geometric — no filesystem, no OS, no network access.
# Blocked: STEPControl, IGESControl, OSD, Storage, PCDM, TDocStd, Interface,
#          IFSelect, XCAFDoc, Resource — all of which expose file I/O.
OCP_ALLOWLIST = frozenset({
    # Geometric primitives
    "OCP.gp",
    # Topology
    "OCP.TopAbs",
    "OCP.TopExp",
    "OCP.TopLoc",
    "OCP.TopTools",
    "OCP.TopoDS",
    # B-rep core
    "OCP.BRep",
    "OCP.BRepTools",
    "OCP.BRepLib",
    # B-rep analysis
    "OCP.BRepAdaptor",
    "OCP.BRepBndLib",
    "OCP.BRepCheck",
    "OCP.BRepClass",
    "OCP.BRepClass3d",
    "OCP.BRepExtrema",
    "OCP.BRepGProp",
    "OCP.BRepIntCurveSurface",
    # B-rep construction
    "OCP.BRepBuilderAPI",
    "OCP.BRepPrimAPI",
    "OCP.BRepFeat",
    "OCP.BRepFilletAPI",
    "OCP.BRepOffsetAPI",
    "OCP.BRepSweep",
    "OCP.BRepProj",
    # B-rep operations
    "OCP.BRepAlgoAPI",
    "OCP.BRepMesh",
    # Geometry
    "OCP.Geom",
    "OCP.Geom2d",
    "OCP.GeomAbs",
    "OCP.GeomAPI",
    "OCP.GeomAdaptor",
    "OCP.GeomConvert",
    "OCP.GeomFill",
    "OCP.GeomLProp",
    "OCP.GeomProjLib",
    "OCP.GeomTools",
    # Adaptors
    "OCP.Adaptor2d",
    "OCP.Adaptor3d",
    # Properties and analysis
    "OCP.GProp",
    "OCP.GCPnts",
    "OCP.Bnd",
    "OCP.IntCurvesFace",
    "OCP.IntTools",
    "OCP.Extrema",
    # Mesh / polygon
    "OCP.Poly",
    # Shape analysis and repair
    "OCP.ShapeAnalysis",
    "OCP.ShapeCustom",
    "OCP.ShapeExtend",
    "OCP.ShapeFix",
    "OCP.ShapeUpgrade",
    # Collection types
    "OCP.TColgp",
    "OCP.TColGeom",
    "OCP.TColStd",
    "OCP.TCollection",
    # Misc safe
    "OCP.MAT",
    "OCP.Approx",
    "OCP.Convert",
    "OCP.BSpl",
    "OCP.ProjLib",
})

# When True, import checks are skipped entirely.  Set via --allow-all-imports.
ALLOW_ALL_IMPORTS: bool = False

# Extra root modules added to the allowlist by the user, on top of IMPORT_ALLOWLIST.
# Set via --allow-imports. Each entry is a top-level module name; submodules of an
# allowed root are permitted (e.g. allowing "scipy" lets "scipy.optimize" through).
EXTRA_ALLOWED_IMPORTS: set[str] = set()


def _is_root_allowed(root: str) -> bool:
    return root in IMPORT_ALLOWLIST or root in EXTRA_ALLOWED_IMPORTS


# ---------------------------------------------------------------------------
# Transitive-safe import check
# ---------------------------------------------------------------------------

# Cache: dotted module name → True (safe) / False (unsafe).
# Pure-Python packages whose full import closure lies within the allowlist
# are permitted without an explicit --allow-imports entry.
_transitive_safe_cache: dict[str, bool] = {}


def _clear_transitive_cache() -> None:
    """Clear the transitive-safety cache. Called in tests."""
    _transitive_safe_cache.clear()


def _source_path(dotted_name: str) -> str | None:
    """Return the .py source file for a module, or None if not pure-Python / not found."""
    try:
        spec = importlib.util.find_spec(dotted_name)
    except Exception:
        return None
    if spec is None or spec.origin is None:
        return None
    if not spec.origin.endswith(".py"):
        return None  # C extension or built-in — no AST to check
    return spec.origin


def _is_transitively_safe(
    dotted_name: str, _visiting: frozenset[str] = frozenset()
) -> bool:
    """Return True if every transitive import of this module is from the allowlist.

    Pure-Python packages whose full import closure stays within
    IMPORT_ALLOWLIST / EXTRA_ALLOWED_IMPORTS are allowed automatically,
    without an explicit --allow-imports entry.  C extensions (no .py source)
    and modules not findable on sys.path are conservatively blocked.
    """
    root = dotted_name.split(".")[0]

    # Explicitly allowed — fast path, no I/O.
    if _is_root_allowed(root):
        return True
    if root == "OCP":
        # Mirror _check_module/_safe_import: only allowed OCP sub-modules are safe.
        ocp_parts = dotted_name.split(".")
        if len(ocp_parts) >= 2:
            return f"OCP.{ocp_parts[1]}" in OCP_ALLOWLIST
        return True  # bare 'OCP'

    # Cache hit.
    cached = _transitive_safe_cache.get(dotted_name)
    if cached is not None:
        return cached

    # Cycle guard: encountering the same module while already checking it
    # means we are in a circular import; return True optimistically — any
    # unsafe dep in the cycle will be caught on the non-cyclic entry path.
    if dotted_name in _visiting:
        return True

    # Must be pure-Python with a findable source file.
    path = _source_path(dotted_name)
    if path is None:
        # Could be a namespace package (directory on sys.path, no __init__.py).
        # Namespace packages have no code to execute — safe as a parent; submodules
        # are checked individually when they're actually imported.
        try:
            spec = importlib.util.find_spec(dotted_name)
            is_ns = (spec is not None and spec.origin is None
                     and spec.submodule_search_locations is not None)
        except Exception:
            is_ns = False
        _transitive_safe_cache[dotted_name] = is_ns
        return is_ns

    # Determine package context so relative imports can be resolved to absolute names.
    # is_package=True  → dotted_name is a package (source file is __init__.py)
    # is_package=False → dotted_name is a module inside a package
    is_package = path.endswith("__init__.py")
    pkg_parts = dotted_name.split(".") if is_package else dotted_name.split(".")[:-1]

    # A submodule's parent package runs its __init__.py at import time with real
    # builtins (outside the restricted exec namespace). Check it before the submodule.
    if not is_package and "." in dotted_name:
        parent = dotted_name.rsplit(".", 1)[0]
        if parent not in _visiting and not _is_transitively_safe(parent, _visiting):
            _transitive_safe_cache[dotted_name] = False
            return False

    try:
        with open(path, encoding="utf-8", errors="replace") as f:  # server-side read; not user-sandbox open
            source = f.read()
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        _transitive_safe_cache[dotted_name] = False
        return False

    visiting = _visiting | {dotted_name}
    for node in tree.body:  # top-level only — skips TYPE_CHECKING guards, try/except optional deps
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_transitively_safe(alias.name, visiting):
                    _transitive_safe_cache[dotted_name] = False
                    return False
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # Resolve relative import to an absolute name and check transitively.
                # `from .mod import X` → loads <pkg>.mod
                # `from . import X`    → loads <pkg>.X
                n_up = node.level - 1  # package levels to ascend above pkg_parts
                if n_up >= len(pkg_parts):
                    # Relative import escapes the root package — block.
                    _transitive_safe_cache[dotted_name] = False
                    return False
                base = pkg_parts[: len(pkg_parts) - n_up]
                if node.module:
                    if not _is_transitively_safe(".".join(base + node.module.split(".")), visiting):
                        _transitive_safe_cache[dotted_name] = False
                        return False
                else:
                    for alias in node.names:
                        if not _is_transitively_safe(".".join(base + [alias.name]), visiting):
                            _transitive_safe_cache[dotted_name] = False
                            return False
            elif node.module and not _is_transitively_safe(node.module, visiting):
                _transitive_safe_cache[dotted_name] = False
                return False

    _transitive_safe_cache[dotted_name] = True
    return True


# Builtins that are dangerous even without an import.
_BLOCKED_BUILTINS = frozenset({
    "eval", "exec", "compile", "open", "breakpoint", "input",
    # getattr/vars/hasattr can bypass the dunder-attribute AST block via string arguments
    # (e.g. getattr(obj, '__class__')). dir() is safe: it only enumerates names already
    # in scope; dunder attribute *access* is still blocked at the AST level.
    "getattr", "vars", "hasattr",
})

# Dunder attributes that are safe to read (no traversal to __subclasses__ etc.
# because those are still blocked).  __class__ is safe: __subclasses__ is still
# blocked, and __init__.__globals__ is also blocked via __globals__ / __init__.
_ALLOWED_DUNDER_ATTRS = frozenset({"__name__", "__doc__", "__class__"})

# Bare-name calls that are caught at the AST level (before exec runs).
_BLOCKED_CALL_NAMES = frozenset({
    "__import__", "eval", "exec", "compile", "open", "breakpoint", "input",
    # Same rationale as _BLOCKED_BUILTINS: getattr/vars/hasattr bypass the dunder check
    # via string arguments. dir() allowed.
    "getattr", "vars", "hasattr",
})


# ---------------------------------------------------------------------------
# Layer 1: AST inspection
# ---------------------------------------------------------------------------

def check_ast(code: str) -> None:
    """Raise ValueError if code contains disallowed imports or dangerous calls.

    Catches the most common injection patterns before exec() is ever called.
    Syntax errors are left for exec() to report with better messages.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return

    if ALLOW_ALL_IMPORTS:
        # Still block dangerous calls even in unrestricted mode.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALL_NAMES:
                    raise ValueError(f"Call to '{node.func.id}' is not allowed.")
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                _check_module(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALL_NAMES:
                raise ValueError(
                    f"Call to '{node.func.id}' is not allowed."
                )
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                if node.attr not in _ALLOWED_DUNDER_ATTRS:
                    raise ValueError(
                        f"Access to dunder attribute '{node.attr}' is not allowed. "
                        f"Use operators and language syntax instead of explicit dunder access. "
                        f"Read-only inspection dunders allowed: {sorted(_ALLOWED_DUNDER_ATTRS)}"
                    )


def _check_module(dotted_name: str) -> None:
    parts = dotted_name.split(".")
    root = parts[0]
    if root == "OCP":
        if len(parts) >= 2:
            ocp_sub = f"OCP.{parts[1]}"
            if ocp_sub not in OCP_ALLOWLIST:
                raise ValueError(
                    f"Import of '{dotted_name}' is not allowed. "
                    f"This OCP sub-module is blocked (potential file I/O or OS access). "
                    f"Permitted OCP modules: {sorted(OCP_ALLOWLIST)}"
                )
        return  # bare 'OCP' or allowed sub-module
    if not _is_root_allowed(root):
        if _is_transitively_safe(dotted_name):
            return  # pure-Python package; full import closure is within the allowlist
        permitted = sorted(IMPORT_ALLOWLIST | EXTRA_ALLOWED_IMPORTS)
        raise ValueError(
            f"Import of '{dotted_name}' is not allowed. "
            f"This blocks filesystem (os, pathlib, shutil), network (socket, urllib, "
            f"requests), and shell access (subprocess). "
            f"Permitted: {permitted}. "
            f"Pure-Python packages on sys.path whose full import closure lies within "
            f"the permitted list above are allowed automatically — no config needed. "
            f"To allow a package with broader dependencies (e.g. one that imports os "
            f"for path handling), use --allow-imports or BUILD123D_ALLOW_IMPORTS env var. "
            f"For project geometry, export to STEP and use import_cad_file() instead."
        )


# ---------------------------------------------------------------------------
# Layer 2: Restricted builtins
# ---------------------------------------------------------------------------

def make_restricted_builtins() -> dict[str, Any]:
    """Return a __builtins__ dict with dangerous functions removed.

    open / eval / exec / compile are removed outright.
    __import__ is replaced with an allowlisted version so that
    'from build123d import *' works but 'import os' is blocked at the
    namespace level even if AST inspection is somehow bypassed.
    """
    import builtins
    safe = vars(builtins).copy()

    for name in _BLOCKED_BUILTINS:
        safe.pop(name, None)

    _original_import = safe["__import__"]

    if ALLOW_ALL_IMPORTS:
        safe["__import__"] = _original_import
        return safe

    def _safe_import(name: str, *args: Any, **kwargs: Any) -> Any:
        parts = name.split(".")
        root = parts[0]
        if root == "OCP":
            if len(parts) >= 2:
                ocp_sub = f"OCP.{parts[1]}"
                if ocp_sub not in OCP_ALLOWLIST:
                    raise ImportError(
                        f"Import of '{name}' is not allowed. "
                        f"This OCP sub-module is blocked (potential file I/O or OS access). "
                        f"Permitted OCP modules: {sorted(OCP_ALLOWLIST)}"
                    )
        elif not _is_root_allowed(root):
            if _is_transitively_safe(name):
                return _original_import(name, *args, **kwargs)
            permitted = sorted(IMPORT_ALLOWLIST | EXTRA_ALLOWED_IMPORTS)
            raise ImportError(
                f"Import of '{name}' is not allowed. "
                f"Permitted: {permitted}. "
                f"Pure-Python packages whose full import closure lies within the "
                f"permitted list are allowed automatically. "
                f"To allow a package with broader dependencies, use --allow-imports "
                f"or BUILD123D_ALLOW_IMPORTS env var. "
                f"For project geometry, export to STEP and use import_cad_file() instead."
            )
        return _original_import(name, *args, **kwargs)

    safe["__import__"] = _safe_import
    return safe


# ---------------------------------------------------------------------------
# Timeout exception (raised by SIGALRM in Session or propagated by WorkerSession)
# ---------------------------------------------------------------------------

class ExecutionTimeout(Exception):
    pass
