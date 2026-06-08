import json
import re

from build123d_mcp.tools.repair_hints import _HINTS

_SUGGESTED_FIXES = {
    "boolean_fail": (
        "Check that both operands are valid solids and their intersection is non-empty; "
        "try visualising each shape separately before the boolean."
    ),
    "syntax_error": (
        "Fix the Python syntax error at the indicated line; "
        "check for missing colons, unmatched brackets, or wrong indentation."
    ),
    "selector_empty": (
        "The selector returned an empty list; "
        "use inspect_drawing() or measure() to list available faces/edges before selecting."
    ),
    "shapelist_attr": (
        "Box() + Cylinder() returns a ShapeList, not a fused Part — it won't have "
        ".volume, .faces(), etc. Fix: wrap in Part(): `Part() + Box(...) + Cylinder(...)`, "
        "or fuse explicitly: `Box(...).fuse(Cylinder(...))`, "
        "or iterate solids: `sum(s.volume for s in result.solids())`."
    ),
    "fillet_fail": (
        "Fillet radius may be too large or the selected edges are non-manifold; "
        "try a smaller radius or select fewer edges."
    ),
    "timeout": (
        "The code exceeded the execution time limit; "
        "break the operation into smaller steps or raise --exec-timeout."
    ),
    "import_blocked": (
        "That import is not in the allowlist; "
        "use only build123d, math, numpy, or other permitted modules."
    ),
    "unknown": (
        "Review the full error message and traceback for clues; "
        "call last_error() for the line number and excerpt."
    ),
}


def _classify_error(exc: Exception, code: str) -> dict:
    """Classify an exception into a failure_class with a suggested fix."""
    from build123d_mcp.security import ExecutionTimeout

    msg = str(exc)
    msg_lower = msg.lower()

    if "Can't get geom adaptor" in msg:
        cls = "boolean_fail"
    elif isinstance(exc, (SyntaxError, IndentationError)):
        cls = "syntax_error"
    elif isinstance(exc, AttributeError) and "ShapeList" in msg:
        cls = "shapelist_attr"
    elif "ShapeList" in msg or ("index" in msg_lower and "faces" in str(exc)):
        cls = "selector_empty"
    elif "fillet" in msg_lower or "non-manifold" in msg_lower:
        cls = "fillet_fail"
    elif isinstance(exc, ExecutionTimeout):
        cls = "timeout"
    elif "not allowed" in msg:
        cls = "import_blocked"
    else:
        cls = "unknown"

    return {"failure_class": cls, "suggested_fix": _SUGGESTED_FIXES[cls]}


def _classify_from_error_string(error_result: str) -> dict:
    """Classify an error string (as returned by session.execute) into a failure_class."""
    msg = error_result
    msg_lower = msg.lower()

    if "Can't get geom adaptor" in msg:
        cls = "boolean_fail"
    elif "SyntaxError" in msg or "IndentationError" in msg:
        cls = "syntax_error"
    elif "AttributeError" in msg and "ShapeList" in msg:
        cls = "shapelist_attr"
    elif "ShapeList" in msg or ("index" in msg_lower and "faces" in msg):
        cls = "selector_empty"
    elif "fillet" in msg_lower or "non-manifold" in msg_lower:
        cls = "fillet_fail"
    elif "ExecutionTimeout" in msg:
        cls = "timeout"
    elif "not allowed" in msg:
        cls = "import_blocked"
    else:
        cls = "unknown"

    return {"failure_class": cls, "suggested_fix": _SUGGESTED_FIXES[cls]}


def execute_code(session, code: str) -> str:
    result = session.execute(code)
    if result.startswith("Error:") or result.startswith("Constraint failed"):
        matched = [hint for patterns, hint in _HINTS if any(re.search(p, result) for p in patterns)]
        if matched:
            result += "\n\nHint: " + ("\n      ".join(matched))

        classification = _classify_from_error_string(result)
        result += "\n\n" + json.dumps(classification)

    return result
