"""Content-addressed STEP analysis and checked mapping back to live faces."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from build123d_mcp.tools._paths import check_input_size, safe_input_path
from build123d_mcp.tools.measure import _resolve_shape

_CACHE_SIZE = 8
_FACE_TOLERANCE = 1e-5


def _step_bytes(shape: Any) -> bytes:
    from build123d_mcp.tools.export import _write_step

    with tempfile.TemporaryDirectory(prefix="build123d_inspect_") as directory:
        path = os.path.join(directory, "model.step")
        _write_step(shape, path)
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
    loaded: Any
    document: dict
    placement: list
    shape: Any
    measures: dict
    _mapped_faces: dict | None = None
    _adjacency: dict | None = None

    @property
    def source_sha256(self) -> str:
        return self.document["binding"]["source_sha256"]

    def bind_id(self, feature_id: str) -> str:
        return f"{feature_id}@{self.source_sha256[:16]}"

    def resolve_id(self, reference: str) -> str:
        feature_id, separator, digest = reference.rpartition("@")
        if not separator or digest != self.source_sha256[:16]:
            raise ValueError(
                "unbound or stale feature id; re-run inspect_features() on the current target"
            )
        if feature_id not in {f["id"] for f in self.document["features"]}:
            raise ValueError(
                "unknown or stale feature id; re-run inspect_features() on the current target"
            )
        return feature_id

    @property
    def mapped_faces(self) -> dict:
        """Map imported geometry only after checking analytic descriptors.

        Unlike editing, adjacency may use a unique geometric match if a loader
        reordered faces. Ambiguous matches are omitted, never guessed.
        """
        if self._mapped_faces is None:
            faces = tuple(self.shape.faces())
            mapped = {}
            for row in self.document["faces"]:
                index = row["id"]
                if index < len(faces) and _face_matches(faces[index], row):
                    mapped[index] = faces[index]
                else:
                    matches = [f for f in faces if _face_matches(f, row)]
                    if len(matches) == 1:
                        mapped[index] = matches[0]
            self._mapped_faces = mapped
        return self._mapped_faces

    @property
    def adjacency(self) -> dict:
        if self._adjacency is None:
            edges: dict = {}
            adjacency: dict[int, set[int]] = {i: set() for i in self.mapped_faces}
            for index, face in self.mapped_faces.items():
                for edge in face.edges():
                    bucket = edges.setdefault(hash(edge), [])
                    for other, owner in bucket:
                        if edge.wrapped.IsSame(other.wrapped):
                            adjacency[index].add(owner)
                            adjacency[owner].add(index)
                    bucket.append((edge, index))
            self._adjacency = adjacency
        return self._adjacency


def _shape_measures(shape) -> dict:
    box = shape.bounding_box()
    return {
        "volume": float(shape.volume),
        "area": float(shape.area),
        "min": list(box.min),
        "max": list(box.max),
        "faces": len(shape.faces()),
    }


def _face_matches(face, descriptor) -> bool:
    from build123d import CenterOf

    centroid = tuple(face.center(CenterOf.MASS))
    kind = str(face.geom_type).rsplit(".", 1)[-1].lower()
    return (
        kind == descriptor["kind"]
        and abs(face.area - descriptor["area"]) <= _FACE_TOLERANCE
        and all(abs(a - b) <= _FACE_TOLERANCE for a, b in zip(centroid, descriptor["centroid"]))
    )


def transform_vector(value, placement, point=False):
    return [
        sum(row[i] * value[i] for i in range(3)) + (row[3] if point else 0) for row in placement
    ]


# Explicit output vocabulary: unknown fields can contain product-frame
# coordinates. Do not pass them through, even when they happen to be scalars.
_POINTS = {"location", "at", "centroid", "origin", "center", "base_point", "top_point"}
_DIRECTIONS = {
    "axis",
    "direction",
    "normal",
    "run",
    "u",
    "v",
    "width_axis",
    "long_axis",
    "other_axis",
    "height_axis",
    "x_axis",
    "y_axis",
}
_POINT_ARRAYS = {"flat_centres", "locations"}
_DIRECTION_ARRAYS = {"flat_directions", "axes"}
_SAFE_FIELDS = {
    "id",
    "family",
    "record",
    "faces",
    "members",
    "kind",
    "type",
    "area",
    "radius",
    "major_radius",
    "minor_radius",
    "semi_angle",
    "diameter",
    "depth",
    "height",
    "width",
    "length",
    "across_flats",
    "thickness",
    "side_count",
    "major_diameter",
    "minor_diameter",
    "drill_diameter",
    "included_angle",
    "angle",
    "size",
    "pitch",
    "x_pitch",
    "y_pitch",
    "rows",
    "columns",
    "count",
    "bottom",
    "cbore",
    "spotface",
    "csink",
    "holes",
    "turned",
    "side",
    "end_radius",
    "corner_radius",
    "edge_anchored",
    "rectangular_boundary",
    "index",
    "body",
    "geometry",
    "frame",
    "classification",
    "feature_kind",
    "section_shape",
    "profile",
    "closure",
    "ends",
    "low",
    "high",
    "condition",
    "surface",
    "evidence",
    "defining_faces",
    "constituent_faces",
}


def _vector(value):
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(v, (int, float)) for v in value)
    )


def _caller_geometry(value, placement):
    """Return only known invariant fields or explicitly transformed geometry.

    Projection scalars (z, spans, positions, body_key, etc.) and unknown fields
    are dropped. Their meaning cannot safely be inferred from a field name.
    Selected analytic faces remain available when a feature record is sparse.
    """
    if isinstance(value, list):
        return [_caller_geometry(v, placement) for v in value]
    if not isinstance(value, dict):
        return value
    value = dict(value)
    axis, center = value.get("axis"), value.get("center")
    if isinstance(axis, str) and axis.lower() in {"x", "y", "z"} and _vector(center):
        index = "xyz".index(axis.lower())
        if all(isinstance(value.get(key), (int, float)) for key in ("base", "top")):
            value.setdefault("height", abs(value["top"] - value["base"]))
        for end in ("base", "top"):
            if isinstance(value.get(end), (int, float)):
                point = list(center)
                point[index] = value.pop(end)
                value[end + "_point"] = point
    out = {}
    for key, item in value.items():
        if key in _DIRECTIONS and isinstance(item, str) and item.lower() in {"x", "y", "z"}:
            item = [float(item.lower() == name) for name in "xyz"]
        if key in _POINTS | _DIRECTIONS:
            if _vector(item):
                out[key] = transform_vector(item, placement, point=key in _POINTS)
        elif key in _POINT_ARRAYS | _DIRECTION_ARRAYS:
            if isinstance(item, list) and all(_vector(v) for v in item):
                out[key] = [
                    transform_vector(v, placement, point=key in _POINT_ARRAYS) for v in item
                ]
        elif key in _SAFE_FIELDS:
            out[key] = _caller_geometry(item, placement)
    return out


def _instance_geometry(data: bytes):
    """Read one STEP instance in memory, retaining its product-to-caller frame."""
    from build123d import Location, Solid
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("XCAF"))
    reader = STEPCAFControl_Reader()
    if reader.ReadStream("model.step", BytesIO(data)) != IFSelect_RetDone or not reader.Transfer(
        doc
    ):
        raise ValueError("Could not import STEP geometry")
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    leaves = []

    def instances(label, parent):
        location = parent.Multiplied(tool.GetLocation_s(label))
        product = label
        if tool.IsReference_s(label):
            product = TDF_Label()
            tool.GetReferredShape_s(label, product)
        if tool.IsAssembly_s(product):
            children = TDF_LabelSequence()
            tool.GetComponents_s(product, children)
            for i in range(1, children.Length() + 1):
                instances(children.Value(i), location)
        else:
            leaves.append((tool.GetShape_s(product), location))

    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        instances(roots.Value(i), TopLoc_Location())
    if len(leaves) != 1:
        raise ValueError("inspection requires one unambiguous STEP instance")
    wrapped, location = leaves[0]
    transform = location.Transformation()
    placement = [[transform.Value(i, j) for j in range(1, 5)] for i in range(1, 4)]
    return Solid.cast(wrapped).moved(Location(location)), placement


def analysis_from_bytes(data: bytes, cache: OrderedDict | None = None) -> Analysis:
    from specify_core import api

    digest = hashlib.sha256(data).hexdigest()
    if cache is not None and digest in cache:
        cache.move_to_end(digest)
        return cache[digest]
    loaded = api.load(data, name="model.step")
    document = api.analyse(loaded)
    # The byte API keeps recognition on this handle. OCCT is used only for
    # caller placement, exact measures and shared-edge/trim evidence, which the
    # public LoadedPart does not expose.
    shape, placement = _instance_geometry(data)
    measures = _shape_measures(shape)
    for key in ("features", "faces"):
        document[key] = _caller_geometry(document[key], placement)
    if document["binding"]["source_sha256"] != digest:
        raise ValueError("STEP analysis binding does not match source_sha256")
    result = Analysis(loaded, document, placement, shape, measures)
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
        if index not in self._descriptors or not 0 <= index < len(self._faces):
            raise ValueError("face mapping mismatch: cannot resolve one exact source face")
        face = self._faces[index]
        if not _face_matches(face, self._descriptors[index]):
            raise ValueError("face mapping mismatch: cannot resolve one exact source face")
        return face


def build_evidence(shape: Any, session=None) -> Evidence:
    cache = session_cache(session) if session is not None else None
    return Evidence(shape, analysis_from_bytes(_step_bytes(shape), cache))
