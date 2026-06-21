#!/usr/bin/env bash
# Run one drawing->solid fixture through Claude Code + gate-equipped build123d-mcp,
# with a live JSON stream you can tail. Portable: no hardcoded machine paths.
#
# Usage:
#   eval/run_fixture.sh <fixture_input_dir> <work_dir> [model] [mcp_spec]
#
#   fixture_input_dir : holds input.png (generation) and optionally input.step (editing)
#   work_dir          : output.step + stream.jsonl + filtered.log land here
#   model             : claude model id (default: claude-opus-4-8)
#   mcp_spec          : build123d-mcp version spec for uvx (default: build123d-mcp@latest)
#
# Live log (in another terminal):
#   tail -n0 -f <work_dir>/stream.jsonl | python3 eval/stream_filter.py <work_dir>
#
# Requires: `claude` (Claude Code) and `uvx` on PATH.
set -euo pipefail
FIX="${1:?fixture input dir}"
WORK="${2:?work dir}"
MODEL="${3:-claude-opus-4-8}"
MCP_SPEC="${4:-build123d-mcp@latest}"
HERE="$(cd "$(dirname "$0")" && pwd)"

command -v claude >/dev/null || { echo "ERROR: 'claude' (Claude Code) not on PATH"; exit 1; }
command -v uvx    >/dev/null || { echo "ERROR: 'uvx' not on PATH"; exit 1; }

mkdir -p "$WORK"
rm -f "$WORK/output.step" "$WORK/stream.jsonl" "$WORK/filtered.log"
cp "$FIX"/input.png  "$WORK"/ 2>/dev/null || true
cp "$FIX"/input.step "$WORK"/ 2>/dev/null || true

OUT="$WORK/output.step"
if [[ -f "$FIX/edit_description.txt" ]]; then
  # editing fixture: bring along the change request + reference renders, and
  # build the editing prompt (literal substitution — the edit text is arbitrary).
  cp "$FIX/edit_description.txt" "$WORK"/ 2>/dev/null || true
  cp -R "$FIX/renders" "$WORK"/ 2>/dev/null || true
  python3 -c "import sys,pathlib; t=pathlib.Path(sys.argv[1]).read_text(); print(t.replace('{EDIT}', pathlib.Path(sys.argv[2]).read_text().strip()).replace('{OUTPUT}', sys.argv[3]), end='')" \
    "$HERE/prompt_editing.txt" "$FIX/edit_description.txt" "$OUT" > "$WORK/prompt.txt"
  TASK="editing"
else
  sed "s|{OUTPUT}|$OUT|g" "$HERE/prompt_generation.txt" > "$WORK/prompt.txt"
  TASK="generation"
fi

cat > "$WORK/mcp_config.json" <<JSON
{"mcpServers":{"build123d":{"command":"uvx","args":["--python","3.12","$MCP_SPEC"]}}}
JSON

echo "fixture: $FIX  ($TASK)"
echo "work:    $WORK"
echo "model:   $MODEL    mcp: $MCP_SPEC"
echo "live:    tail -n0 -f $WORK/stream.jsonl | python3 $HERE/stream_filter.py $WORK"
echo "running claude -p ..."

cd "$WORK"
claude -p "$(cat prompt.txt)" \
  --model "$MODEL" \
  --output-format stream-json --verbose \
  --mcp-config mcp_config.json \
  --strict-mcp-config \
  --dangerously-skip-permissions \
  --disable-slash-commands \
  --allowedTools "mcp__build123d__execute,mcp__build123d__render_view,mcp__build123d__measure,mcp__build123d__validate,mcp__build123d__export,mcp__build123d__import_cad_file,mcp__build123d__save_snapshot,mcp__build123d__restore_snapshot,mcp__build123d__find_holes,mcp__build123d__find_hole_patterns" \
  > stream.jsonl 2>&1

echo
if [[ -f output.step ]]; then
  echo "output.step produced ($(wc -c < output.step) bytes)"
else
  echo "NO output.step (timeout or the agent stopped early) — see stream.jsonl"
fi
