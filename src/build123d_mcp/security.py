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
        permitted = sorted(IMPORT_ALLOWLIST | EXTRA_ALLOWED_IMPORTS)
        raise ValueError(
            f"Import of '{dotted_name}' is not allowed. "
            f"This blocks filesystem (os, pathlib, shutil), network (socket, urllib, "
            f"requests), and shell access (subprocess). "
            f"Permitted: {permitted}. "
            f"To allow project-local packages, add their top-level name to the "
            f"server's --allow-imports flag or BUILD123D_ALLOW_IMPORTS env var "
            f"(e.g. --allow-imports my_package). The package must be on PYTHONPATH "
            f"or installed in the server's environment. Transitive dependencies of "
            f"the allowed package also need to be on the allowlist or in the stdlib "
            f"safe list above."
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
            permitted = sorted(IMPORT_ALLOWLIST | EXTRA_ALLOWED_IMPORTS)
            raise ImportError(
                f"Import of '{name}' is not allowed. "
                f"Permitted: {permitted}. "
                f"To allow project-local packages, add their top-level name to the "
                f"server's --allow-imports flag or BUILD123D_ALLOW_IMPORTS env var "
                f"(e.g. --allow-imports my_package). The package must be on PYTHONPATH "
                f"or installed in the server's environment. Transitive dependencies of "
                f"the allowed package also need to be on the allowlist or in the stdlib "
                f"safe list above."
            )
        return _original_import(name, *args, **kwargs)

    safe["__import__"] = _safe_import
    return safe


# ---------------------------------------------------------------------------
# Timeout exception (raised by SIGALRM in Session or propagated by WorkerSession)
# ---------------------------------------------------------------------------

class ExecutionTimeout(Exception):
    pass
