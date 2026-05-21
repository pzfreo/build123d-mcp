import json


def script(session, save_to: str = "") -> str:
    """Join all successfully executed code blocks into a single script.

    Prepends 'from build123d import *' if not already present in the first block.
    If save_to is given, writes the script to that path.

    Returns:
        JSON: {script, blocks} or {script_path, blocks}
    """
    blocks = list(getattr(session, "execute_history", []))
    n = len(blocks)

    if n == 0:
        joined = ""
    else:
        # Prepend import if not already present in first block
        first = blocks[0]
        if "from build123d import" not in first and "import build123d" not in first:
            blocks = ["from build123d import *"] + blocks
        joined = "\n\n".join(blocks)

    if save_to:
        try:
            with open(save_to, "w", encoding="utf-8") as f:
                f.write(joined)
        except OSError as exc:
            return json.dumps({"error": f"Failed to write script: {exc}", "path": save_to})
        return json.dumps({"script_path": save_to, "blocks": n}, indent=2)

    return json.dumps({"script": joined, "blocks": n}, indent=2)
