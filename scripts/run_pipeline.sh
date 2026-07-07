#!/usr/bin/env bash
# Thin shim: orchestration lives in nagare_clip.pipeline (Python).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
exec uv run --project "$PROJECT_ROOT" python -m nagare_clip.pipeline "$@"
