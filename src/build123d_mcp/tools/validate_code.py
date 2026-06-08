import ast
import json

from build123d_mcp.security import _BLOCKED_CALL_NAMES, IMPORT_ALLOWLIST


def validate_code(code: str) -> str:
    # Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return json.dumps(
            {
                "syntax": f"SyntaxError at line {e.lineno}: {e.msg}",
                "blocked": [],
                "warnings": [],
                "ok": False,
            },
            indent=2,
        )

    blocked = []
    warnings = []

    # Security: blocked imports and calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in IMPORT_ALLOWLIST:
                    blocked.append(f"import '{alias.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root not in IMPORT_ALLOWLIST:
                    blocked.append(f"import '{node.module}' is not allowed")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALL_NAMES:
                blocked.append(f"call to '{node.func.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                blocked.append(f"dunder attribute access '{node.attr}' is not allowed")

    # Advisory: no build123d import in this snippet
    has_b3d_import = any(
        (isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("build123d"))
        or (isinstance(n, ast.Import) and any(a.name.startswith("build123d") for a in n.names))
        for n in ast.walk(tree)
    )
    if not has_b3d_import:
        warnings.append(
            "no build123d import in this snippet — ok if already imported in a prior execute()"
        )

    # Advisory: no result variable or show() call
    has_output = any(
        (
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "result" for t in n.targets)
        )
        or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "show")
        for n in ast.walk(tree)
    )
    if not has_output:
        warnings.append(
            "no 'result' assignment or show() call — the session won't capture a shape unless current_shape is set by other means"
        )

    return json.dumps(
        {
            "syntax": "ok",
            "blocked": blocked,
            "warnings": warnings,
            "ok": len(blocked) == 0,
        },
        indent=2,
    )
