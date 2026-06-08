import json
import types

from build123d_mcp.tools.diff import _collect

_SKIP = {"__builtins__", "show"}


def _is_imported_symbol(val) -> bool:
    """Return True if val is a class/function/module imported from build123d, not a user value."""
    if isinstance(val, types.ModuleType):
        return True
    mod = getattr(val, "__module__", "") or ""
    if mod.startswith("build123d") or mod.startswith("cadquery") or mod.startswith("OCP"):
        if isinstance(val, type) or (callable(val) and not isinstance(val, type)):
            return True
    return False


def _build123d_public_names() -> set[str]:
    try:
        import build123d

        return set(dir(build123d))
    except ImportError:
        return set()


_BUILD123D_NAMES: set[str] | None = None


def _namespace_summary(namespace: dict) -> dict:
    global _BUILD123D_NAMES
    if _BUILD123D_NAMES is None:
        _BUILD123D_NAMES = _build123d_public_names()

    _shape_cls: type | None = None
    try:
        from build123d import Shape

        _shape_cls = Shape
    except ImportError:
        pass

    result = {}
    for name, val in namespace.items():
        if name.startswith("_") or name in _SKIP:
            continue
        if _is_imported_symbol(val):
            continue
        if name in _BUILD123D_NAMES:
            continue
        try:
            typ = type(val).__name__
            if _shape_cls is not None and isinstance(val, _shape_cls):
                try:
                    result[name] = {"type": typ, "volume": round(val.volume, 4)}  # type: ignore[attr-defined]
                except Exception:
                    result[name] = {"type": typ}
            elif isinstance(val, (list, tuple)):
                result[name] = {"type": typ, "length": len(val)}
            elif isinstance(val, dict):
                result[name] = {"type": "dict", "length": len(val)}
            elif isinstance(val, bool):
                result[name] = {"type": "bool", "value": val}
            elif isinstance(val, (int, float)):
                result[name] = {"type": typ, "value": val}
            elif isinstance(val, str):
                result[name] = {"type": "str", "value": val[:80]}
            elif callable(val):
                result[name] = {"type": "function"}
            else:
                result[name] = {"type": typ}
        except Exception:
            pass
    return result


def session_state(session) -> str:
    state = _collect(session.current_shape, session.objects)
    state["snapshots"] = list(session.snapshots.keys())
    state["variables"] = _namespace_summary(session.namespace)
    state["geometry_refs"] = dict(getattr(session, "geometry_refs", {}))
    return json.dumps(state, indent=2)
