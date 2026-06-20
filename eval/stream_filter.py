"""Turn `claude -p --output-format stream-json` (on stdin) into a readable live log.

Writes every event concisely to <work_dir>/filtered.log (full timeline) and
prints high-signal events to stdout (tool calls, validate/export results, errors,
agent reasoning) so a stuck validate-loop or repeated error is visible live.

Usage:
  tail -n0 -f <work_dir>/stream.jsonl | python3 eval/stream_filter.py <work_dir>
"""

import json
import os
import sys
import time

work = sys.argv[1] if len(sys.argv) > 1 else "."
LOG = os.path.join(work, "filtered.log")
t0 = time.time()
n_build = 0


def stamp():
    return f"[{int(time.time() - t0):4d}s]"


def emit(line, key=False):
    with open(LOG, "a") as f:
        f.write(line + "\n")
    if key:
        print(line, flush=True)


for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        ev = json.loads(raw)
    except Exception:
        continue
    t = ev.get("type")
    if t == "assistant":
        for b in ev.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                emit(f"{stamp()} chat: {b['text'].strip()[:200]}", key=True)
            elif b.get("type") == "tool_use":
                name = b.get("name", "").replace("mcp__build123d__", "")
                inp = b.get("input", {}) or {}
                if name == "execute":
                    n_build += 1
                    code = (inp.get("code", "") or "").replace("\n", " ")[:90]
                    emit(f"{stamp()} execute #{n_build}: {code}", key=(n_build % 5 == 1))
                elif name in ("validate", "export"):
                    arg = inp.get("object_name", inp.get("filename", ""))
                    emit(f"{stamp()} >> {name}({arg})", key=True)
                else:
                    emit(f"{stamp()} . {name}", key=False)
    elif t == "user":
        for b in ev.get("message", {}).get("content", []):
            if b.get("type") == "tool_result":
                c = b.get("content", "")
                txt = (
                    c
                    if isinstance(c, str)
                    else " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                )
                txt = txt.replace("\n", " ").strip()
                low = txt.lower()
                key = any(
                    k in low
                    for k in (
                        "validity gate",
                        "exported",
                        "gate fail",
                        "error",
                        "warning",
                        "non-manifold",
                        "open edge",
                    )
                )
                emit(f"{stamp()}   <- {txt[:160]}", key=key)
    elif t == "result":
        secs = ev.get("duration_ms", 0) // 1000
        emit(f"{stamp()} RESULT ({secs}s): {str(ev.get('result', ''))[:160]}", key=True)
