#!/usr/bin/env bash
# Deterministic publish tools for the agent skill (no LLM call).
# Run from the VIDEO directory, like director_tools.sh.
#
#   ./scripts/publish_tools.sh view   --config nagare_config.yml [--lines 120-240]
#   ./scripts/publish_tools.sh frames --config nagare_config.yml [--line N[@pos]]...
#   ./scripts/publish_tools.sh write  --config nagare_config.yml --edit edit.json [--no-render]
#   ./scripts/publish_tools.sh render --config nagare_config.yml
#
# Line numbers are the director's display numbering. `write` validates
# everything and writes nothing on any problem.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
exec uv run --project "$PROJECT_ROOT" python -m nagare_clip.publish.tools "$@"
