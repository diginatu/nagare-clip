#!/usr/bin/env bash
# Append a turn to the plan stage's conversation (output/plan_dialogue/history.md).
#
#   ./scripts/plan_say.sh "PXL_1234 [31,83] は 60-83 だけがデモ本体、33-59 は脱線"
#   echo "..." | ./scripts/plan_say.sh
#
# Then re-run just the plan stage (one LLM call) to have it applied:
#   ./scripts/run_pipeline.sh --from-stage plan --to-stage plan
#
# Editing the file by hand under a "## human" heading works exactly as well.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
exec uv run --project "$PROJECT_ROOT" python -m nagare_clip.plan.dialogue "$@"
