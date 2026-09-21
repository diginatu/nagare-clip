#!/usr/bin/env bash
# Print what the director's ops will play, per segment (read-only, no LLM call).
#
#   ./scripts/director_preview.sh --config nagare_config.yml
#   ./scripts/director_preview.sh --config nagare_config.yml --source PXL_1234.mp4
#   ./scripts/director_preview.sh --config nagare_config.yml \
#       --director-dir /tmp/old-run/director --report-dir /tmp/old-run/llm_report_director
#
# Transcripts, timings, silences and gap descriptions come from the project's
# output dir exactly as the director stage loads them; --director-dir swaps in a
# different set of {stem}_director.json files to compare old results.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
exec uv run --project "$PROJECT_ROOT" python -m nagare_clip.director.preview_cli "$@"
