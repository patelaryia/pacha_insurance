#!/usr/bin/env bash
# Weekly architecture drift job. Report only — never opens a PR.
#
#   loop/drift.sh              real run
#   loop/drift.sh --dry-run    print what it would do, touch nothing
#
# The dry-run check is the FIRST thing after argument parsing. The previous
# version created directories, fetched, checked out main and pulled before
# it looked at --dry-run, so its dry run mutated the working tree.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

LAST_RUN="loop/drift/LAST_RUN"
since=""
[[ -f "$LAST_RUN" ]] && since="$(cat "$LAST_RUN")"
report="loop/drift/$(date -u +%Y-%m-%d).md"

if ((DRY_RUN)); then
  echo "drift dry run — nothing below is executed"
  echo "  window:    ${since:-no previous run recorded; would use the last 30 days}"
  echo "  WOULD: mkdir -p loop/drift"
  echo "  WOULD: git fetch origin main && git checkout main && git pull --ff-only"
  echo "  WOULD: claude -p /drift --permission-mode acceptEdits"
  echo "  WOULD: write $report"
  echo "  WOULD: git rev-parse HEAD > $LAST_RUN"
  exit 0
fi

mkdir -p loop/drift
touch "$LAST_RUN"
since="$(cat "$LAST_RUN")"

git fetch --quiet origin main
git checkout --quiet main
git pull --ff-only --quiet

if [[ -z "$since" ]]; then
  echo "no previous run recorded — window is the last 30 days"
  range=(--since=30.days)
else
  echo "window: $since..HEAD"
  range=("$since..HEAD")
fi

commits="$(git log --oneline "${range[@]}" | wc -l | tr -d ' ')"
echo "$commits commits in window"
if [[ "$commits" == "0" ]]; then
  echo "nothing merged since the last drift run — no report"
  exit 0
fi

claude -p "/drift" --permission-mode acceptEdits

if [[ ! -f "$report" ]]; then
  # Do not advance the watermark on a run that produced nothing: the next
  # run must still cover this window rather than skipping it silently.
  echo "drift job produced no report at $report — window not recorded" >&2
  exit 1
fi
git rev-parse HEAD > "$LAST_RUN"
echo "wrote $report"
