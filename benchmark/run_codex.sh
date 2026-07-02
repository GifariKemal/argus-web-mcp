#!/usr/bin/env bash
# Run Codex CLI (native web_search) over the stratified COMPARE_IDS and save
# one raw answer per scenario to benchmark/codex_compare/<id>.txt. Idempotent: skips
# any id that already has a non-empty output file, so it can resume after a stop.
#
# Usage:  bash benchmark/run_codex.sh
# Prereqs: `codex` on PATH; the Argus venv python for reading scenarios.
set -uo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$BENCH_DIR")"
OUT_DIR="$BENCH_DIR/codex_compare"
PY="$ROOT_DIR/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT_DIR/.venv/bin/python"   # fall back to POSIX venv layout

mkdir -p "$OUT_DIR"

# Emit "<id>\t<query>" lines for the compare scenarios.
mapfile -t LINES < <(
  "$PY" -c "import sys; sys.path.insert(0,'$BENCH_DIR'); import scenarios as s; [print(i + chr(9) + s.by_id(i)['query']) for i in s.COMPARE_IDS]"
)

total=${#LINES[@]}
if [ "$total" -eq 0 ]; then
  echo "ERROR: could not read scenarios (python=$PY)" >&2
  exit 1
fi
echo "Codex run over $total compare scenarios -> $OUT_DIR"

i=0
for line in "${LINES[@]}"; do
  i=$((i + 1))
  id="${line%%$'\t'*}"
  query="${line#*$'\t'}"
  out="$OUT_DIR/$id.txt"

  if [ -s "$out" ]; then
    echo "  [$i/$total] $id - skip (already have non-empty output)"
    continue
  fi

  echo "  [$i/$total] $id - $query"
  prompt="$query - search the web; list the top sources (titles+URLs) and a one-line summary."
  codex exec \
    -c web_search="live" \
    --skip-git-repo-check \
    --sandbox read-only \
    "$prompt" > "$out" 2>"$out.err"

  if [ ! -s "$out" ]; then
    echo "    [warn] empty output for $id (see $out.err)" >&2
  fi
  sleep 2
done

echo "done: $(ls -1 "$OUT_DIR"/*.txt 2>/dev/null | wc -l) output files in $OUT_DIR"
