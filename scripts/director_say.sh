#!/usr/bin/env bash
# Tell the director something (output/director/conversation.md).
#
#   ./scripts/director_say.sh "冒頭のあいさつは残して。配管の速回しは長すぎる"
#   echo "..." | ./scripts/director_say.sh
#
# It removes the done mark and appends an "## editor" entry — exactly what you
# can do by hand in the file. Then re-run from the director:
#   ./scripts/run_pipeline.sh --from-stage director
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
exec uv run --project "$PROJECT_ROOT" python -m nagare_clip.director.conversation "$@"
