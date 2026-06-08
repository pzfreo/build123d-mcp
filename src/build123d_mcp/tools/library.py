import ast
import json
import os
import time
from typing import Any


def _extract_part_info(source: str) -> dict:
    """Extract PART_INFO dict from module source using AST literal_eval (no exec)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PART_INFO":
                    try:
                        return ast.literal_eval(node.value)
                    except Exception:
                        pass
    return {}


class _LibraryIndex:
    def __init__(self, library_path: str):
        self.library_path = os.path.realpath(library_path)
        self._index: dict = {}
        self._last_scan: float = 0.0

    def _needs_rescan(self) -> bool:
        for dirpath, _, filenames in os.walk(self.library_path):
            # Directory mtime changes on add/remove; check it before file stats.
            if os.path.getmtime(dirpath) > self._last_scan:
                return True
            for filename in filenames:
                if filename.endswith(".py"):
                    if os.path.getmtime(os.path.join(dirpath, filename)) > self._last_scan:
                        return True
        return False

    def ensure_fresh(self):
        if self._needs_rescan():
            self._scan()

    def _scan(self):
        new_index = {}
        # Track the largest mtime we observe so _last_scan is provably >=
        # everything we just indexed. Without this, Windows file-mtime
        # granularity (NTFS rounds mtime up; time.time() returns the raw
        # clock) can leave dir mtimes > time.time() at scan completion,
        # making _needs_rescan() return True on the very next call. See #99.
        max_mtime = 0.0
        for dirpath, _, filenames in os.walk(self.library_path):
            try:
                max_mtime = max(max_mtime, os.path.getmtime(dirpath))
            except OSError:
                pass
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                filepath = os.path.join(dirpath, filename)
                stem = os.path.splitext(filename)[0]
                rel = os.path.relpath(dirpath, self.library_path)
                category = "" if rel == "." else rel
                name = f"{category}/{stem}" if category else stem
                try:
                    with open(filepath) as f:
                        source = f.read()
                    info = _extract_part_info(source)
                except Exception as e:
                    info = {"description": f"Error reading part: {e}", "tags": [], "parameters": {}}
                file_mtime = os.path.getmtime(filepath)
                max_mtime = max(max_mtime, file_mtime)
                new_index[name] = {
                    "name": name,
                    "category": category,
                    "path": filepath,
                    "mtime": file_mtime,
                    "description": info.get("description", ""),
                    "tags": info.get("tags", []),
                    "parameters": info.get("parameters", {}),
                }
        self._index = new_index
        self._last_scan = max(time.time(), max_mtime)

    def search(self, query: str) -> list:
        self.ensure_fresh()
        parts = self._index.values()
        if not query.strip():
            return [_public(p) for p in parts]
        terms = query.lower().split()
        results = []
        for part in parts:
            text = " ".join(
                [
                    part["name"],
                    part["description"],
                    part["category"],
                    " ".join(part["tags"]),
                ]
            ).lower()
            if all(term in text for term in terms):
                results.append(_public(part))
        return results

    def get(self, name: str):
        self.ensure_fresh()
        return self._index.get(name)


def _public(part: dict) -> dict:
    return {
        "name": part["name"],
        "category": part["category"],
        "description": part["description"],
        "tags": part["tags"],
        "parameters": part["parameters"],
    }


def search_library(index: _LibraryIndex, query: str = "") -> str:
    results = index.search(query)
    if not results:
        msg = f"No parts found matching '{query}'." if query.strip() else "Part library is empty."
        return msg
    return json.dumps(results, indent=2)


def load_part(session, index: _LibraryIndex, name: str, params: str = "") -> str:
    entry = index.get(name)
    if entry is None:
        available = sorted(index._index.keys())
        raise ValueError(f"Unknown part '{name}'. Available: {available}")

    overrides = {}
    if params.strip():
        try:
            overrides = json.loads(params)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid params JSON: {e}")

    # Merge defaults from PART_INFO with caller overrides
    final_params = {}
    for param_name, spec in entry["parameters"].items():
        final_params[param_name] = overrides.pop(param_name, spec.get("default"))
    if overrides:
        raise ValueError(
            f"Unknown parameters: {list(overrides.keys())}. "
            f"Expected: {list(entry['parameters'].keys())}"
        )

    # Execute part file in isolated restricted namespace
    from build123d_mcp.security import check_ast, make_restricted_builtins

    with open(entry["path"]) as f:
        source = f.read()
    check_ast(source)
    namespace: dict[str, Any] = {"__builtins__": make_restricted_builtins()}
    exec(compile(source, entry["path"], "exec"), namespace)  # noqa: S102

    if "make" not in namespace:
        raise ValueError(f"Part '{name}' has no make() function.")

    shape = namespace["make"](**final_params)

    session.objects[name] = shape
    session.current_shape = shape

    try:
        vol = shape.volume
        faces = len(shape.faces())
        return f"Loaded '{name}': volume={vol:.4g} mm³, faces={faces}. Parameters: {final_params}"
    except Exception:
        return f"Loaded '{name}'. Parameters: {final_params}"
