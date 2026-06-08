import json


def list_objects(session) -> str:
    if not session.objects:
        return "No named objects in session. Use show(shape, name) to register shapes."
    results = []
    for name, shape in session.objects.items():
        try:
            results.append(
                {
                    "name": name,
                    "volume": round(shape.volume, 4),
                    "faces": len(shape.faces()),
                    "edges": len(shape.edges()),
                    "vertices": len(shape.vertices()),
                }
            )
        except Exception as e:
            results.append({"name": name, "error": str(e)})
    return json.dumps(results, indent=2)
