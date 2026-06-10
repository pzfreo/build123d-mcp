import json


def analyze_printability(
    session,
    object_name: str = "",
    support_angle: float = 45.0,
    nozzle: float = 0.4,
    min_perimeters: int = 2,
    build_volume: str = "",
    bed_tol: float = 0.001,
    min_feature: float = 0.5,
) -> str:
    """Run augura printability analysis on a named session object."""
    import augura

    if object_name:
        if object_name not in session.objects:
            return json.dumps(
                {
                    "error": f"Unknown object '{object_name}'. Registered: {list(session.objects.keys())}"
                }
            )
        shape = session.objects[object_name]
    elif session.current_shape is not None:
        shape = session.current_shape
    else:
        return json.dumps({"error": "No shape in session. Execute code to create geometry first."})

    bv: tuple[float, float, float] | None = None
    if build_volume.strip():
        parts = build_volume.strip().split()
        if len(parts) != 3:
            return json.dumps({"error": "build_volume must be 'X Y Z' (three numbers in mm)"})
        try:
            bv = (float(parts[0]), float(parts[1]), float(parts[2]))
        except ValueError:
            return json.dumps({"error": "build_volume must be 'X Y Z' (three numbers in mm)"})

    report = augura.analyze(
        shape,
        support_angle=support_angle,
        nozzle=nozzle,
        min_perimeters=min_perimeters,
        build_volume=bv,
        bed_tol=bed_tol,
        min_feature=min_feature,
    )

    data = report.to_dict()

    # Prepend a plain-text digest so LLM agents get an immediate readable summary.
    counts: dict[str, int] = {}
    for f in report.findings:
        counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
    summary_parts = [f"{v} {k}" for k, v in sorted(counts.items())]
    total = len(report.findings)
    summary = (
        f"{total} finding(s): {', '.join(summary_parts)}"
        if summary_parts
        else "0 findings — part looks printable"
    )

    return summary + "\n\n" + json.dumps(data)
