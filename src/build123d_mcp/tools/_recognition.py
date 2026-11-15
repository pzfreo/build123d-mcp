"""Content-addressed STEP analysis and checked mapping back to live faces."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from build123d_mcp.tools._paths import check_input_size, safe_input_path
from build123d_mcp.tools.measure import _resolve_shape

_CACHE_SIZE = 8
_FACE_TOLERANCE = 1e-5


def _step_bytes(shape: Any) -> bytes:
    from build123d import export_step

    with tempfile.TemporaryDirectory(prefix="build123d_inspect_") as directory:
        path = os.path.join(directory, "model.step")
        if not export_step(shape, path):
            raise ValueError("could not export the shape to STEP for inspection")
        with open(path, "rb") as stream:
            data = stream.read()
    # OCCT writes a clock timestamp even for identical geometry. Canonicalise
    # only this header metadata, also on build123d versions without timestamp=.
    return re.sub(
        rb"(FILE_NAME\s*\(\s*'(?:[^']|'')*'\s*,\s*)'(?:[^']|'')*'",
        rb"\1'2000-01-01T00:00:00'",
        data,
        count=1,
    )


@dataclass
class Analysis:
    step: bytes
    loaded: Any
    document: dict
    placement: list
    caller_bounds: tuple | None = None
    _measures: dict | None = None

    @property
    def source_sha256(self) -> str:
        return self.document["binding"]["source_sha256"]

    @property
    def measures(self) -> dict:
        # 0.1.15 publishes measures only through diff, not analyse. Lazily ask
        # the engine once; retaining LoadedPart also retains its native cache.
        if self._measures is None:
            from specify_core import api

            self._measures = self.caller_measures(
                api.diff(self.loaded, self.loaded)["report"]["measures"]["old"]
            )
        return self._measures

    def caller_measures(self, measures: dict) -> dict:
        out = copy.deepcopy(measures)
        if self.caller_bounds is not None:
            out["min"], out["max"] = self.caller_bounds
        else:
            shift = [row[3] for row in self.placement]
            for key in ("min", "max"):
                out[key] = [a + b for a, b in zip(out[key], shift)]
        return out


def transform_vector(value, placement, point=False):
    return [
        sum(row[i] * value[i] for i in range(3)) + (row[3] if point else 0) for row in placement
    ]


def _caller_geometry(value, placement):
    if isinstance(value, list):
        return [_caller_geometry(v, placement) for v in value]
    if not isinstance(value, dict):
        return value
    value = dict(value)
    axis = value.get("axis")
    center = value.get("center")
    if isinstance(axis, str) and axis.lower() in {"x", "y", "z"} and isinstance(center, list):
        index = "xyz".index(axis.lower())
        for end in ("base", "top"):
            if isinstance(value.get(end), (int, float)):
                point = list(center)
                point[index] = value.pop(end)
                value[end + "_point"] = point
    out = {}
    for key, item in value.items():
        if (
            key in {"axis", "width_axis", "long_axis"}
            and isinstance(item, str)
            and item.lower() in {"x", "y", "z"}
        ):
            out[key] = transform_vector([float(item.lower() == axis) for axis in "xyz"], placement)
        elif (
            key
            in {
                "location",
                "centroid",
                "origin",
                "center",
                "axis",
                "direction",
                "normal",
                "base_point",
                "top_point",
            }
            and isinstance(item, list)
            and len(item) == 3
            and all(isinstance(v, (int, float)) for v in item)
        ):
            out[key] = transform_vector(
                item,
                placement,
                point=key
                in {"location", "centroid", "origin", "center", "base_point", "top_point"},
            )
        else:
            out[key] = _caller_geometry(item, placement)
    return out


def analysis_from_bytes(data: bytes, cache: OrderedDict | None = None) -> Analysis:
    from specify_core import api

    digest = hashlib.sha256(data).hexdigest()
    if cache is not None and digest in cache:
        cache.move_to_end(digest)
        return cache[digest]
    with tempfile.TemporaryDirectory(prefix="build123d_inspect_") as directory:
        path = os.path.join(directory, "model.step")
        with open(path, "wb") as stream:
            stream.write(data)
        loaded = api.load(path)
        document = api.analyse(loaded)
        # The engine analyses a product in its local frame; STEP instance
        # placement is separate, including for a single translated live solid.
        part = api.parts(path)[loaded.binding.part]
        if len(part["placements"]) != 1:
            raise ValueError("inspection requires one unambiguous STEP instance")
        placement = part["placements"][0]
        bounds = None
        rotated = any(
            abs(placement[i][j] - float(i == j)) > 1e-10 for i in range(3) for j in range(3)
        )
        if rotated:
            # Reuse the existing STEP importer for exact caller AABBs; rotating
            # a product-space AABB would inflate a rotated cylinder's envelope.
            from build123d_mcp.tools.import_step import _load_step

            box = _load_step(path).bounding_box()
            bounds = (list(box.min), list(box.max))
        document = copy.deepcopy(document)
        for key in ("features", "faces"):
            document[key] = _caller_geometry(document[key], placement)
    if document["binding"]["source_sha256"] != digest:
        raise ValueError("STEP analysis binding does not match source_sha256")
    result = Analysis(data, loaded, document, placement, bounds)
    if cache is not None:
        cache[digest] = result
        while len(cache) > _CACHE_SIZE:
            cache.popitem(last=False)
    return result


def session_cache(session) -> OrderedDict:
    if not hasattr(session, "_step_analysis_cache"):
        session._step_analysis_cache = OrderedDict()
    return session._step_analysis_cache


def resolve_analysis(session, target: str = "") -> Analysis:
    if not target or target in session.objects:
        shape = _resolve_shape(session, target)
        if not shape.is_valid or not shape.solids():
            raise ValueError(
                "Inspection needs a valid solid; repair it first "
                "(build123d://skill/repair; validate() must pass)."
            )
        data = _step_bytes(shape)
    else:
        path = safe_input_path(target)
        check_input_size(path, "cad")
        if os.path.splitext(path)[1].lower() not in {".step", ".stp"}:
            raise ValueError("target must be a registered object name or a .step/.stp path")
        with open(path, "rb") as stream:
            data = stream.read()
    return analysis_from_bytes(data, session_cache(session))


def _type_name(family: str) -> str:
    if family.endswith("ies"):
        singular = family[:-3] + "y"
    elif family.endswith(("sses", "shes", "ches", "xes")):
        singular = family[:-2]
    elif family.endswith("s"):
        singular = family[:-1]
    else:
        singular = family
    return "".join(part.capitalize() for part in singular.split("_"))


class Record(dict):
    kind: str = ""

    def to_dict(self) -> dict:
        return dict(self)


class Evidence:
    """Analysis of the current STEP bytes, with verified live face access."""

    def __init__(self, shape: Any, analysis: Analysis):
        self._shape = shape
        self.analysis = analysis
        self._faces = tuple(shape.faces())
        self._descriptors = {f["id"]: f for f in analysis.document["faces"]}
        self._features = {f["id"]: f for f in analysis.document["features"]}
        self.features = list(self._features)
        if len(self._faces) != len(self._descriptors):
            raise ValueError("face count mismatch; evidence cannot map to one exact source face")

    def family(self, feature: str) -> str:
        return self._features[feature]["family"]

    def record(self, feature: str) -> Record:
        record = Record(self._features[feature]["record"])
        record.kind = _type_name(self.family(feature))
        return record

    def constituent_faces(self, feature: str) -> list[int]:
        return list(self._features[feature]["faces"])

    def face(self, index: int) -> Any:
        """Refuse reordered STEP indices unless kind, centroid AND area agree."""
        from build123d import CenterOf

        if index not in self._descriptors or not 0 <= index < len(self._faces):
            raise ValueError("face mapping mismatch: cannot resolve one exact source face")
        face = self._faces[index]
        descriptor = self._descriptors[index]
        centroid = tuple(face.center(CenterOf.MASS))
        kind = str(face.geom_type).rsplit(".", 1)[-1].lower()
        if (
            kind != descriptor["kind"]
            or abs(face.area - descriptor["area"]) > _FACE_TOLERANCE
            or any(abs(a - b) > _FACE_TOLERANCE for a, b in zip(centroid, descriptor["centroid"]))
        ):
            raise ValueError("face mapping mismatch: cannot resolve one exact source face")
        return face


def build_evidence(shape: Any, session=None) -> Evidence:
    cache = session_cache(session) if session is not None else None
    return Evidence(shape, analysis_from_bytes(_step_bytes(shape), cache))
