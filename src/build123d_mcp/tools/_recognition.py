"""Adapter from quiddity 0.4's file-based API to the live session shape.

quiddity 0.4 recognises STEP, not build123d objects: it returns JSON whose faces
are zero-based indices into the file. Each run exports the shape once, keeps those
bytes, and resolves an index to the live face with the same index — verified
against quiddity's own proved point on that face before it is handed out, so an
export that renumbered faces is refused rather than silently misattributed.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

# Families that aggregate other records rather than claim faces of their own.
_AGGREGATE_SUFFIXES = ("_patterns", "_refusals")
_ANCHOR_TOLERANCE = 1e-5


def _step_bytes(shape: Any) -> bytes:
    from build123d import export_step

    fd, path = tempfile.mkstemp(suffix=".step")
    os.close(fd)
    try:
        if not export_step(shape, path):
            raise ValueError("could not export the shape to STEP for recognition")
        with open(path, "rb") as stream:
            return stream.read()
    finally:
        os.unlink(path)


def _type_name(family: str) -> str:
    """``section_recesses`` -> ``SectionRecess``: the record type the family holds."""
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
    """A feature record. ``to_dict()`` and ``kind`` keep the 0.3 call shape."""

    kind: str = ""

    def to_dict(self) -> dict:
        return dict(self)


class Evidence:
    """One recognition run over one shape: inventory, features and face mapping."""

    def __init__(self, shape: Any, framed: bool = False):
        import quiddity

        self._shape = shape
        self._step = _step_bytes(shape)
        try:
            document = json.loads(quiddity.evidence(self._step, framed=framed))
        except ValueError as exc:
            if framed:
                raise ValueError(f"part-relative evidence refused: {exc}") from exc
            raise
        envelope = json.loads(quiddity.recognise(self._step))

        self._faces = tuple(shape.faces())
        if document["face_count"] != len(self._faces):
            raise ValueError(
                f"recognition saw {document['face_count']} faces but the shape has "
                f"{len(self._faces)}; face evidence cannot be mapped"
            )
        self._features: list[dict] = document["features"]
        self.features = list(range(len(self._features)))
        self.frame: dict | None = document.get("frame")
        self.complete = document.get("complete", True)
        self.failures = document.get("failures", [])
        # Recognition records per family, including aggregates such as hole_patterns.
        self.result: dict[str, list] = {
            name: value for name, value in envelope.items() if isinstance(value, list)
        }
        self._verified: set[int] = set()

    # -- per-feature accessors -------------------------------------------------
    def family(self, feature: int) -> str:
        return self._features[feature]["family"]

    def record(self, feature: int) -> Record:
        record = Record(self._features[feature]["record"])
        record.kind = _type_name(self.family(feature))
        return record

    def is_aggregate(self, feature: int) -> bool:
        entry = self._features[feature]
        return bool(entry.get("members")) or entry["family"].endswith(_AGGREGATE_SUFFIXES)

    def defining_faces(self, feature: int) -> list[int]:
        return list(self._features[feature]["defining_faces"])

    def constituent_faces(self, feature: int) -> list[int]:
        return list(self._features[feature]["constituent_faces"])

    def face(self, index: int) -> Any:
        """The live face at ``index``, checked against quiddity's proved anchor point."""
        if not 0 <= index < len(self._faces):
            raise ValueError("recognition evidence did not resolve to one exact source face")
        face = self._faces[index]
        if index not in self._verified:
            import quiddity
            from build123d import Vector

            point = Vector(*json.loads(quiddity.anchor(self._step, index))["point"])
            if face.distance_to(point) > _ANCHOR_TOLERANCE:
                raise ValueError("recognition evidence did not resolve to one exact source face")
            self._verified.add(index)
        return face

    # -- whole-run summaries ---------------------------------------------------
    def association(self) -> dict[str, Any]:
        """How much of the part the accepted features account for, by faces and area."""
        claimed: set[int] = set()
        for entry in self._features:
            claimed.update(entry["constituent_faces"])
        areas = [face.area for face in self._faces]
        total_area = sum(areas)
        claimed_area = sum(areas[i] for i in claimed if i < len(areas))
        total = len(self._faces)
        return {
            "faces": {
                "total": total,
                "associated": len(claimed),
                "unassociated": total - len(claimed),
                "ratio": round(len(claimed) / total, 6) if total else 0.0,
            },
            "surface_area": {
                "total": round(total_area, 6),
                "associated": round(claimed_area, 6),
                "unassociated": round(total_area - claimed_area, 6),
                "ratio": round(claimed_area / total_area, 6) if total_area else 0.0,
            },
        }


def build_evidence(shape: Any, framed: bool = False) -> Evidence:
    return Evidence(shape, framed=framed)
