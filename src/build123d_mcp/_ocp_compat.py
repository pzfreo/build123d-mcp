"""OCP 7.x / 8.x compatibility shims (#488).

OCP 8 (build123d 0.13+) moved the NCollection instantiations out of
``OCP.TopTools`` into ``OCP.collections`` and turned ``TopoDS`` into a namespace,
so the static downcasts lost their ``_s`` suffix (``TopoDS.Face_s`` →
``TopoDS.Face``). Import the version-neutral names from here instead.

The old names are tried first, so behaviour on OCP 7.x is unchanged.
"""

from OCP.TopoDS import TopoDS

try:
    from OCP.TDF import TDF_LabelSequence as LabelSequence
    from OCP.TopTools import (
        TopTools_HSequenceOfShape as HSequenceOfShape,
    )
    from OCP.TopTools import (
        TopTools_IndexedDataMapOfShapeListOfShape as IndexedDataMapOfShapeListOfShape,
    )
    from OCP.TopTools import (
        TopTools_IndexedMapOfShape as IndexedMapOfShape,
    )
except ImportError:  # OCP 8+
    from OCP.collections import (  # type: ignore[no-redef]
        HSequence_TopoDS_Shape as HSequenceOfShape,
    )
    from OCP.collections import (  # type: ignore[no-redef]
        IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as IndexedDataMapOfShapeListOfShape,
    )
    from OCP.collections import (  # type: ignore[no-redef]
        IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as IndexedMapOfShape,
    )
    from OCP.collections import (  # type: ignore[no-redef]
        Sequence_TDF_Label as LabelSequence,
    )


def _downcast(name: str):
    return getattr(TopoDS, f"{name}_s", None) or getattr(TopoDS, name)


to_face = _downcast("Face")
to_edge = _downcast("Edge")
to_vertex = _downcast("Vertex")
to_wire = _downcast("Wire")
to_solid = _downcast("Solid")
to_shell = _downcast("Shell")

__all__ = [
    "HSequenceOfShape",
    "IndexedDataMapOfShapeListOfShape",
    "IndexedMapOfShape",
    "LabelSequence",
    "to_edge",
    "to_face",
    "to_shell",
    "to_solid",
    "to_vertex",
    "to_wire",
]
