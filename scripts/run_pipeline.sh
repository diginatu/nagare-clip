#!/usr/bin/env bash

set -euo pipefail

# Resolve the project root directory (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  echo "Usage: ./scripts/run_pipeline.sh [OPTIONS] <language>"
  echo ""
  echo "Options:"
  echo "  --source            FILE  Source video file (may be repeated; default: all videos in input-videos-dir)"
  echo "  --config            FILE  Path to YAML config file"
  echo "  --input-videos-dir  DIR   Directory containing source videos (default: src_video)"
  echo "  --output-dir        DIR   Directory for all output artifacts (default: output)"
  echo "  --pre-margin        SEC   Seconds to extend keep intervals before start (default: 1.0)"
  echo "  --post-margin       SEC   Seconds to extend keep intervals after end (default: 1.0)"
  echo "  --align-model       MODEL HuggingFace model ID for WhisperX alignment"
  echo "                            Japanese default: vumichien/wav2vec2-large-xlsr-japanese"
  echo "                            English default: (whisperx built-in)"
  echo ""
  echo "Positional:"
  echo "  language                  Language code, e.g. ja, en"
}

CONFIG_FILE=""
INPUT_VIDEOS_DIR=""
OUTPUT_DIR=""
PRE_MARGIN=""
POST_MARGIN=""
ALIGN_MODEL=""

# Track which values were explicitly set on CLI
CLI_INPUT_VIDEOS_DIR=""
CLI_OUTPUT_DIR=""
CLI_PRE_MARGIN=""
CLI_POST_MARGIN=""
CLI_ALIGN_MODEL=""
CLI_SILENCE_THRESHOLD=""
CLI_MIN_KEEP=""
CLI_SOURCES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) CLI_SOURCES+=("$2"); shift 2 ;;
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --input-videos-dir) CLI_INPUT_VIDEOS_DIR="$2"; shift 2 ;;
    --output-dir) CLI_OUTPUT_DIR="$2"; shift 2 ;;
    --pre-margin) CLI_PRE_MARGIN="$2"; shift 2 ;;
    --post-margin) CLI_POST_MARGIN="$2"; shift 2 ;;
    --align-model) CLI_ALIGN_MODEL="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    *) break ;;
  esac
done

LANGUAGE="${1:-}"

if [[ -z "$LANGUAGE" ]]; then
  usage >&2
  exit 1
fi

# --- Resolve config file values for pipeline/stage1 settings ---
CFG_INPUT_VIDEOS_DIR=""
CFG_OUTPUT_DIR=""
CFG_PRE_MARGIN=""
CFG_POST_MARGIN=""
CFG_ALIGN_MODEL=""
CFG_SILENCE_THRESHOLD=""
CFG_MIN_KEEP=""
CFG_COMPUTE_TYPE=""
CFG_BATCH_SIZE=""

if [[ -n "$CONFIG_FILE" ]]; then
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Config file not found: $CONFIG_FILE" >&2
    exit 1
  fi
  eval "$(uv run --project "$PROJECT_ROOT" python3 -c "
import yaml, sys, shlex
with open(sys.argv[1]) as f:
    c = yaml.safe_load(f) or {}
s1 = c.get('stage1', {})
s2 = c.get('stage2', {})
p  = c.get('pipeline', {})
def out(name, val):
    if val is not None and val != '':
        print(f'{name}={shlex.quote(str(val))}')
out('CFG_COMPUTE_TYPE', s1.get('compute_type'))
out('CFG_BATCH_SIZE', s1.get('batch_size'))
out('CFG_ALIGN_MODEL', s1.get('align_model'))
out('CFG_SILENCE_THRESHOLD', s2.get('silence_threshold'))
out('CFG_MIN_KEEP', s2.get('min_keep'))
out('CFG_PRE_MARGIN', s2.get('pre_margin'))
out('CFG_POST_MARGIN', s2.get('post_margin'))
out('CFG_INPUT_VIDEOS_DIR', p.get('input_videos_dir'))
out('CFG_OUTPUT_DIR', p.get('output_dir'))
" "$CONFIG_FILE")"
fi

# Precedence: CLI > config > defaults
INPUT_VIDEOS_DIR="${CLI_INPUT_VIDEOS_DIR:-${CFG_INPUT_VIDEOS_DIR:-src_video}}"
OUTPUT_DIR="${CLI_OUTPUT_DIR:-${CFG_OUTPUT_DIR:-output}}"
PRE_MARGIN="${CLI_PRE_MARGIN:-${CFG_PRE_MARGIN:-1.0}}"
POST_MARGIN="${CLI_POST_MARGIN:-${CFG_POST_MARGIN:-1.0}}"
ALIGN_MODEL="${CLI_ALIGN_MODEL:-${CFG_ALIGN_MODEL:-}}"
SILENCE_THRESHOLD="${CLI_SILENCE_THRESHOLD:-${CFG_SILENCE_THRESHOLD:-1.5}}"
MIN_KEEP="${CLI_MIN_KEEP:-${CFG_MIN_KEEP:-1.0}}"
COMPUTE_TYPE="${CFG_COMPUTE_TYPE:-float16}"
BATCH_SIZE="${CFG_BATCH_SIZE:-16}"

# Set default alignment model per language if not specified
if [[ -z "$ALIGN_MODEL" ]]; then
  case "$LANGUAGE" in
    ja) ALIGN_MODEL="vumichien/wav2vec2-large-xlsr-japanese" ;;
  esac
fi

mkdir -p "$INPUT_VIDEOS_DIR" "$OUTPUT_DIR" "$PROJECT_ROOT/cache"

ABS_INPUT_VIDEOS="$(realpath "$INPUT_VIDEOS_DIR")"
ABS_OUTPUT_DIR="$(realpath "$OUTPUT_DIR")"

# --- Source file discovery ---
SOURCE_PATHS=()

if [[ ${#CLI_SOURCES[@]} -gt 0 ]]; then
  # Explicit --source flags: resolve each path
  for src in "${CLI_SOURCES[@]}"; do
    if [[ "$src" == */* ]]; then
      SOURCE_PATHS+=("$src")
    else
      SOURCE_PATHS+=("${INPUT_VIDEOS_DIR%/}/$src")
    fi
  done
else
  # Auto-discover all video files in input-videos-dir, sorted alphabetically
  while IFS= read -r -d '' f; do
    SOURCE_PATHS+=("$f")
  done < <(find "$INPUT_VIDEOS_DIR" -maxdepth 1 \
    \( -iname "*.mp4" -o -iname "*.mkv" -o -iname "*.mov" \
       -o -iname "*.avi" -o -iname "*.webm" \) \
    -print0 | sort -z)

  if [[ ${#SOURCE_PATHS[@]} -eq 0 ]]; then
    echo "No video files found in: $INPUT_VIDEOS_DIR" >&2
    exit 1
  fi
fi

# Validate all source files exist
for src in "${SOURCE_PATHS[@]}"; do
  if [[ ! -f "$src" ]]; then
    echo "Source file not found: $src" >&2
    exit 1
  fi
done

# Build config passthrough args for Python stages
CONFIG_ARGS=()
if [[ -n "$CONFIG_FILE" ]]; then
  CONFIG_ARGS=("--config" "$(realpath "$CONFIG_FILE")")
fi

# Build Stage 2 CLI override args (only explicitly-set values)
STAGE2_OVERRIDE_ARGS=()
if [[ -n "$CLI_SILENCE_THRESHOLD" ]]; then
  STAGE2_OVERRIDE_ARGS+=("--silence_threshold" "$CLI_SILENCE_THRESHOLD")
fi
if [[ -n "$CLI_MIN_KEEP" ]]; then
  STAGE2_OVERRIDE_ARGS+=("--min_keep" "$CLI_MIN_KEEP")
fi
if [[ -n "$CLI_PRE_MARGIN" ]]; then
  STAGE2_OVERRIDE_ARGS+=("--pre_margin" "$CLI_PRE_MARGIN")
fi
if [[ -n "$CLI_POST_MARGIN" ]]; then
  STAGE2_OVERRIDE_ARGS+=("--post_margin" "$CLI_POST_MARGIN")
fi

# Build align model args
ALIGN_MODEL_ARGS=()
if [[ -n "$ALIGN_MODEL" ]]; then
  ALIGN_MODEL_ARGS=("--align_model" "$ALIGN_MODEL")
fi

# --- Stage 1 & 2: loop over each source ---
ALL_SOURCE_PATHS=()
ALL_INTERVALS=()
CLEANUP_COPIES=()
FIRST_STEM=""

for SOURCE_PATH in "${SOURCE_PATHS[@]}"; do
  ABS_SOURCE="$(realpath "$SOURCE_PATH")"

  if [[ "$ABS_SOURCE" == "$ABS_INPUT_VIDEOS/"* ]]; then
    SOURCE_RELATIVE="${ABS_SOURCE#"$ABS_INPUT_VIDEOS/"}"
  else
    cp "$SOURCE_PATH" "$INPUT_VIDEOS_DIR/"
    SOURCE_RELATIVE="$(basename "$SOURCE_PATH")"
    CLEANUP_COPIES+=("${INPUT_VIDEOS_DIR}/$(basename "$SOURCE_PATH")")
  fi

  BASENAME="$(basename "$SOURCE_PATH")"
  STEM="${BASENAME%.*}"
  [[ -z "$FIRST_STEM" ]] && FIRST_STEM="$STEM"

  WHISPER_JSON="${OUTPUT_DIR}/${STEM}.json"
  INTERVALS_JSON="${OUTPUT_DIR}/${STEM}_intervals.json"

  echo "[Stage 1/3] WhisperX transcription: $BASENAME"
  INPUT_VIDEOS_DIR="$ABS_INPUT_VIDEOS" OUTPUT_DIR="$ABS_OUTPUT_DIR" \
  docker compose -f "$PROJECT_ROOT/docker-compose.yml" run --rm --user "0:0" whisperx \
    _ \
    "$SOURCE_RELATIVE" \
    --output_dir /output \
    --output_format all \
    --language "$LANGUAGE" \
    --compute_type "$COMPUTE_TYPE" \
    --batch_size "$BATCH_SIZE" \
    "${ALIGN_MODEL_ARGS[@]}"

  echo "[Stage 2/3] Keep interval computation: $BASENAME"
  uv run --project "$PROJECT_ROOT" python -m nagare_clip.cli \
    --json "$WHISPER_JSON" \
    "${CONFIG_ARGS[@]}" \
    "${STAGE2_OVERRIDE_ARGS[@]}" \
    --output "$INTERVALS_JSON"

  ALL_SOURCE_PATHS+=("$ABS_SOURCE")
  ALL_INTERVALS+=("$(realpath "$INTERVALS_JSON")")
done

# --- Stage 3: Blender VSE project generation ---
BLEND_OUTPUT="${OUTPUT_DIR}/${FIRST_STEM}_edited.blend"

STAGE3_SOURCE_ARGS=()
for src in "${ALL_SOURCE_PATHS[@]}"; do
  STAGE3_SOURCE_ARGS+=("--source" "$src")
done

STAGE3_INTERVALS_ARGS=()
for ivp in "${ALL_INTERVALS[@]}"; do
  STAGE3_INTERVALS_ARGS+=("--intervals" "$ivp")
done

echo "[Stage 3/3] Blender VSE project generation"
blender --background --factory-startup --python-exit-code 1 --python "$PROJECT_ROOT/src/nagare_clip/stage3/blender_cli.py" -- \
  "${STAGE3_SOURCE_ARGS[@]}" \
  "${STAGE3_INTERVALS_ARGS[@]}" \
  --output "$BLEND_OUTPUT" \
  "${CONFIG_ARGS[@]}"

# Cleanup any copied source files
for f in "${CLEANUP_COPIES[@]}"; do
  rm -f "$f"
done

echo "Done: $BLEND_OUTPUT"
