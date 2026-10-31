#!/usr/bin/env bash
# Deterministic director tools for the agent skill (no LLM call).
#
#   ./scripts/director_tools.sh view    --config nagare_config.yml [--lines 120-240] [--no-context]
#   ./scripts/director_tools.sh dump    --config nagare_config.yml > edit.json
#   ./scripts/director_tools.sh preview --config nagare_config.yml [--edit edit.json]
#   ./scripts/director_tools.sh write   --config nagare_config.yml --edit edit.json
#
# Line numbers are the display numbering `view` prints. `preview` and `view`
# never write; `write` validates everything and writes nothing on any problem.
# See .apm/skills/nagare-director/SKILL.md and docs/stages/director_skill.md.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
exec uv run --project "$PROJECT_ROOT" python -m nagare_clip.director.tools "$@"
