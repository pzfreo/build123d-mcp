import json
import os
import tempfile


def health_check(_session) -> str:
    results: dict = {}

    # PNG render (VTK + display)
    try:
        from build123d import Box

        from build123d_mcp.tools.render import _QUALITY, _do_render_png

        img_bytes, _warnings = _do_render_png(
            [("test", Box(1, 1, 1), None)], _QUALITY["standard"], "iso", "", None, 0.0, 0.0
        )
        results["render_png"] = {"ok": True, "bytes": len(img_bytes)}
    except Exception as e:
        results["render_png"] = {"ok": False, "error": str(e)}

    # SVG render (build123d HLR projection)
    try:
        from build123d import Box

        from build123d_mcp.tools.render import _do_render_svg

        svg = _do_render_svg([("test", Box(1, 1, 1), None)], "iso", "", None, 0.0, 0.0)
        results["render_svg"] = {"ok": True, "bytes": len(svg)}
    except Exception as e:
        results["render_svg"] = {"ok": False, "error": str(e)}

    # STEP export
    try:
        from build123d import Box, export_step

        fd, path = tempfile.mkstemp(suffix=".step")
        os.close(fd)
        export_step(Box(1, 1, 1), path)
        results["export_step"] = {"ok": True, "bytes": os.path.getsize(path)}
        os.unlink(path)
    except Exception as e:
        results["export_step"] = {"ok": False, "error": str(e)}

    # STL export
    try:
        from build123d import Box, Mesher

        fd, path = tempfile.mkstemp(suffix=".stl")
        os.close(fd)
        m = Mesher()
        m.add_shape(Box(1, 1, 1))
        m.write(path)
        results["export_stl"] = {"ok": True, "bytes": os.path.getsize(path)}
        os.unlink(path)
    except Exception as e:
        results["export_stl"] = {"ok": False, "error": str(e)}

    results["ok"] = all(v["ok"] for v in results.values() if isinstance(v, dict) and "ok" in v)
    return json.dumps(results, indent=2)
