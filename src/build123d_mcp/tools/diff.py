def _shape_diag(shape) -> dict:
    bb = shape.bounding_box()
    return {
        "volume": round(shape.volume, 4),
        "faces": len(shape.faces()),
        "edges": len(shape.edges()),
        "vertices": len(shape.vertices()),
        "bbox": [round(bb.size.X, 4), round(bb.size.Y, 4), round(bb.size.Z, 4)],
    }


def _collect(current_shape, objects: dict) -> dict:
    result: dict = {"current_shape": None, "objects": {}}
    if current_shape is not None:
        try:
            result["current_shape"] = _shape_diag(current_shape)
        except Exception as e:
            result["current_shape"] = {"error": str(e)}
    for name, shape in objects.items():
        try:
            result["objects"][name] = _shape_diag(shape)
        except Exception as e:
            result["objects"][name] = {"error": str(e)}
    return result


def _fmt_shape_diff(a: dict | None, b: dict | None, label: str) -> str | None:
    if a is None and b is None:
        return None
    if a is None:
        assert b is not None
        return f"  {label}: (none) → volume={b['volume']} mm³, {b['faces']}f {b['edges']}e {b['vertices']}v"
    if b is None:
        return f"  {label}: volume={a['volume']} mm³ → (none)"
    lines = [f"  {label}:"]
    dv = b["volume"] - a["volume"]
    lines.append(f"    volume: {a['volume']} → {b['volume']} mm³  (Δ {dv:+.4f})")
    if (a["faces"], a["edges"], a["vertices"]) != (b["faces"], b["edges"], b["vertices"]):
        lines.append(
            f"    topology: {a['faces']}/{a['edges']}/{a['vertices']} → "
            f"{b['faces']}/{b['edges']}/{b['vertices']} (f/e/v)"
        )
    if a["bbox"] != b["bbox"]:
        av, bv = a["bbox"], b["bbox"]
        lines.append(f"    bbox: {av[0]}×{av[1]}×{av[2]} → {bv[0]}×{bv[1]}×{bv[2]} mm")
    return "\n".join(lines)


def diff_snapshot(session, snapshot_a: str, snapshot_b: str = "", format: str = "text") -> str:
    if snapshot_a not in session.snapshots:
        return (
            f"Error: no snapshot named '{snapshot_a}'. Available: {list(session.snapshots.keys())}"
        )

    snap_a = session.snapshots[snapshot_a]
    diag_a = _collect(snap_a["current_shape"], snap_a["objects"])

    label_b = snapshot_b or "current"
    if snapshot_b:
        if snapshot_b not in session.snapshots:
            return f"Error: no snapshot named '{snapshot_b}'. Available: {list(session.snapshots.keys())}"
        snap_b = session.snapshots[snapshot_b]
        diag_b = _collect(snap_b["current_shape"], snap_b["objects"])
    else:
        diag_b = _collect(session.current_shape, session.objects)

    if format == "json":
        import json

        return json.dumps(
            {"a": {"label": snapshot_a, **diag_a}, "b": {"label": label_b, **diag_b}}, indent=2
        )

    lines = [f"diff: {snapshot_a} → {label_b}", ""]

    cs_diff = _fmt_shape_diff(diag_a["current_shape"], diag_b["current_shape"], "current_shape")
    if cs_diff:
        lines.append(cs_diff)
        lines.append("")

    all_names = sorted(set(list(diag_a["objects"].keys()) + list(diag_b["objects"].keys())))
    if all_names:
        lines.append("objects:")
        for name in all_names:
            a_obj = diag_a["objects"].get(name)
            b_obj = diag_b["objects"].get(name)
            if a_obj is None:
                lines.append(f"  + {name} (added): volume={b_obj['volume']} mm³, {b_obj['faces']}f")
            elif b_obj is None:
                lines.append(f"  - {name} (removed): was volume={a_obj['volume']} mm³")
            else:
                dv = b_obj["volume"] - a_obj["volume"]
                if abs(dv) > 0.001 or a_obj["faces"] != b_obj["faces"]:
                    lines.append(
                        f"  ~ {name}: volume {a_obj['volume']} → {b_obj['volume']} mm³"
                        f"  (Δ {dv:+.4f}), faces {a_obj['faces']} → {b_obj['faces']}"
                    )
                else:
                    lines.append(f"  = {name}: unchanged (volume={a_obj['volume']} mm³)")

    return "\n".join(lines)
