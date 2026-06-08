"""Introspect the installed bd_warehouse package and return a plain-text catalogue.

If bd_warehouse is not installed the resource returns a short explanation.
"""

from __future__ import annotations

import importlib
import inspect
import re

# Standard build123d parameters — not useful to show in the catalogue.
_SKIP_PARAMS = frozenset({"self", "rotation", "align", "mode", "hand", "simple"})

# Modules that contain base/abstract classes we don't want to highlight.
_BASE_CLASS_NAMES = frozenset({"Screw", "Nut", "Washer", "Bearing", "Thread", "Flange"})


def _first_docline(obj) -> str:
    doc = inspect.getdoc(obj) or ""
    return doc.split("\n")[0].strip()


def _sig_summary(cls) -> str:
    """Return a compact constructor signature, omitting boilerplate parameters."""
    try:
        sig = inspect.signature(cls.__init__)
    except (ValueError, TypeError):
        return "(…)"
    parts = []
    for name, p in sig.parameters.items():
        if name in _SKIP_PARAMS:
            continue
        if p.default is inspect.Parameter.empty:
            parts.append(name)
        else:
            d = p.default
            # Shorten Mode/Align defaults
            ds = repr(d)
            if "Mode." in ds:
                ds = ds.split(".")[-1].rstrip(">").strip()
            elif "Align." in ds:
                ds = ds.split(".")[-1].rstrip(">").strip()
            parts.append(f"{name}={ds}")
    return f"({', '.join(parts)})"


def _extract_literal_values(annotation_str: str) -> list[str]:
    """Extract values from a Literal[...] annotation string."""
    m = re.search(r"Literal\[(.+)\]", annotation_str)
    if not m:
        return []
    return re.findall(r"'([^']+)'", m.group(1))


def _sizes_for_type(cls, type_value: str) -> list[str]:
    try:
        return sorted(cls.sizes(type_value))
    except Exception:
        return []


def _format_class(cls) -> str | None:
    """Return a formatted entry for one class, or None to skip."""
    sig = inspect.signature(cls.__init__) if hasattr(cls, "__init__") else None
    lines = [f"{cls.__name__}{_sig_summary(cls)}"]

    doc = _first_docline(cls)
    if doc and doc.lower() != cls.__name__.lower():
        lines.append(f"  {doc}")

    # Find type-selector parameters (fastener_type, bearing_type, etc.)
    if sig:
        for pname, p in sig.parameters.items():
            if pname in _SKIP_PARAMS:
                continue
            ann = str(p.annotation)
            if "Literal[" not in ann:
                continue
            type_values = _extract_literal_values(ann)
            if not type_values:
                continue
            lines.append(f"  {pname}: {', '.join(type_values)}")
            # Show sizes for the default type (first / most common)
            default_type = p.default if isinstance(p.default, str) else type_values[0]
            sizes = _sizes_for_type(cls, default_type)
            if sizes:
                shown = ", ".join(sizes[:12])
                suffix = ", …" if len(sizes) > 12 else ""
                lines.append(f"  sizes ({default_type}): {shown}{suffix}")

    return "\n".join(lines)


def build_bd_warehouse_text() -> str:
    try:
        import bd_warehouse  # noqa: F401, PLC0415  # imported only to probe availability
    except ImportError:
        return (
            "bd_warehouse is not installed.\n"
            "Install with: pip install bd_warehouse\n"
            "See https://github.com/gumyr/bd_warehouse for documentation."
        )

    module_labels = {
        "bd_warehouse.bearing": "Bearings",
        "bd_warehouse.fastener": "Fasteners",
        "bd_warehouse.flange": "Flanges",
        "bd_warehouse.gear": "Gears",
        "bd_warehouse.open_builds": "OpenBuilds parts",
        "bd_warehouse.pipe": "Pipes",
        "bd_warehouse.sprocket": "Sprockets",
        "bd_warehouse.thread": "Threads",
    }

    _USAGE_PREAMBLE = """\
BD_WAREHOUSE COMPONENTS
=======================
Pre-built parametric parts for build123d.
Import each class from the module shown. All dimensions in mm unless noted.

SIZE STRING FORMAT
  Use 'M6-1' not 'M6-1.0'. Always probe before scripting:
    from bd_warehouse.fastener import CounterSunkScrew
    print(CounterSunkScrew.sizes("iso10642"))
  This shows all valid strings and avoids format errors at build time.

HOLE OPERATIONS  (from bd_warehouse.fastener import ...)
  These accept a fastener object and derive all geometry from its ISO data.
  Never compute countersink or tap-drill dimensions manually — use these instead.

  CounterSinkHole(fastener, depth, fit='Normal', counter_sink_angle=82)
    Correctly-sized countersunk hole for flat/CSK screws. Example:
      screw = CounterSunkScrew(size='M6-1', fastener_type='iso10642', length=10)
      with BuildPart() as p:
          Cylinder(radius=20, height=10)
          CounterSinkHole(fastener=screw, depth=10)

  TapHole(fastener, depth, fit='Normal', include_threads=False)
    Tapped bore at the correct ISO tap-drill diameter.
    Set include_threads=True to add modelled helical thread geometry.
      TapHole(fastener=screw, depth=10, include_threads=True)

  ClearanceHole(fastener, depth, fit='Normal', counter_sunk=False)
    Through-hole on a mating part; fit controls close/normal/loose clearance.
      ClearanceHole(fastener=screw, depth=10)

  CounterBoreHole(fastener, depth, fit='Normal')
    Counterbored hole for socket-head cap screws.
      bolt = SocketHeadCapScrew(size='M6-1', fastener_type='iso4762', length=20)
      CounterBoreHole(fastener=bolt, depth=20)\
"""

    sections: list[str] = [_USAGE_PREAMBLE]

    for mod_name, label in module_labels.items():
        try:
            m = importlib.import_module(mod_name)
        except ImportError:
            continue

        short = mod_name.split(".")[-1]
        entries: list[str] = []

        classes = [
            (name, obj)
            for name, obj in inspect.getmembers(m, inspect.isclass)
            if obj.__module__ == mod_name and not name.startswith("_")
        ]

        for name, cls in classes:
            if name in _BASE_CLASS_NAMES:
                continue
            entry = _format_class(cls)
            if entry:
                entries.append(entry)

        if not entries:
            continue

        header = f"## {label}  (from bd_warehouse.{short} import ClassName)"
        sections.append(header + "\n\n" + "\n\n".join(entries))

    return "\n\n".join(sections)
