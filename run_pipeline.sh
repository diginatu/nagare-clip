#!/usr/bin/env bash

set -euo pipefail

usage() {
  echo "Usage: ./run_pipeline.sh [--input-videos-dir DIR] [--output-dir DIR] [--pre-margin SEC] [--post-margin SEC] [--align-model MODEL] <source> <language> [silence_threshold] [min_keep]"
  echo "  --input-videos-dir  DIR  Directory containing source videos (default: src_video)"
  echo "  --output-dir        DIR  Directory for all output artifacts (default: output)"
  echo "  --pre-margin        SEC  Seconds to extend keep intervals before start (default: 1.0)"
  echo "  --post-margin       SEC  Seconds to extend keep intervals after end (default: 1.0)"
  echo "  --align-model       MODEL  HuggingFace model ID for WhisperX alignment"
  echo "                             Japanese default: vumichien/wav2vec2-large-xlsr-japanese"
  echo "                             English default: (whisperx built-in)"
  echo "  source                   Video filename (resolved under input-videos-dir) or explicit path"
  echo "  language                 Language code, e.g. ja, en"
}

INPUT_VIDEOS_DIR="src_video"
OUTPUT_DIR="output"
PRE_MARGIN="${PRE_MARGIN:-1.0}"
POST_MARGIN="${POST_MARGIN:-1.0}"
ALIGN_MODEL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input-videos-dir) INPUT_VIDEOS_DIR="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --pre-margin) PRE_MARGIN="$2"; shift 2 ;;
    --post-margin) POST_MARGIN="$2"; shift 2 ;;
    --align-model) ALIGN_MODEL="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    *) break ;;
  esac
done

SOURCE_ARG="${1:-}"
LANGUAGE="${2:-}"
SILENCE_THRESHOLD="${3:-1.5}"
MIN_KEEP="${4:-1.0}"

if [[ -z "$SOURCE_ARG" || -z "$LANGUAGE" ]]; then
  usage >&2
  exit 1
fi

# Set default alignment model per language if not specified
if [[ -z "$ALIGN_MODEL" ]]; then
  case "$LANGUAGE" in
    ja) ALIGN_MODEL="vumichien/wav2vec2-large-xlsr-japanese" ;;
  esac
fi

if [[ "$SOURCE_ARG" == */* ]]; then
  SOURCE_PATH="$SOURCE_ARG"
else
  SOURCE_PATH="${INPUT_VIDEOS_DIR%/}/$SOURCE_ARG"
fi

if [[ ! -f "$SOURCE_PATH" ]]; then
  echo "Source file not found: $SOURCE_PATH"
  exit 1
fi

mkdir -p "$INPUT_VIDEOS_DIR" "$OUTPUT_DIR" cache config

ABS_INPUT_VIDEOS="$(realpath "$INPUT_VIDEOS_DIR")"
ABS_SOURCE="$(realpath "$SOURCE_PATH")"
ABS_OUTPUT_DIR="$(realpath "$OUTPUT_DIR")"

if [[ "$ABS_SOURCE" == "$ABS_INPUT_VIDEOS/"* ]]; then
  SOURCE_RELATIVE="${ABS_SOURCE#"$ABS_INPUT_VIDEOS/"}"
else
  cp "$SOURCE_PATH" "$INPUT_VIDEOS_DIR/"
  SOURCE_RELATIVE="$(basename "$SOURCE_PATH")"
  CLEANUP_COPY=true
fi

BASENAME="$(basename "$SOURCE_PATH")"
STEM="${BASENAME%.*}"

WHISPER_JSON="${OUTPUT_DIR}/${STEM}.json"
INTERVALS_JSON="${OUTPUT_DIR}/${STEM}_intervals.json"
BLEND_OUTPUT="${OUTPUT_DIR}/${STEM}_edited.blend"

# Build optional align_model flag
ALIGN_MODEL_ARGS=()
if [[ -n "$ALIGN_MODEL" ]]; then
  ALIGN_MODEL_ARGS=("--align_model" "$ALIGN_MODEL")
fi

echo "[Stage 1/3] WhisperX transcription"
INPUT_VIDEOS_DIR="$ABS_INPUT_VIDEOS" OUTPUT_DIR="$ABS_OUTPUT_DIR" \
docker compose run --rm --user "0:0" whisperx \
  _ \
  "$SOURCE_RELATIVE" \
  --output_dir /output \
  --output_format all \
  --language "$LANGUAGE" \
  --compute_type float16 \
  --batch_size 16 \
  "${ALIGN_MODEL_ARGS[@]}"

echo "[Stage 2/3] Keep interval computation"
uv run python stage2_intervals.py \
  --json "$WHISPER_JSON" \
  --config config/filler_words.yaml \
  --language "$LANGUAGE" \
  --silence_threshold "$SILENCE_THRESHOLD" \
  --min_keep "$MIN_KEEP" \
  --pre_margin "$PRE_MARGIN" \
  --post_margin "$POST_MARGIN" \
  --output "$INTERVALS_JSON"

echo "[Stage 3/3] Blender VSE project generation"
blender --background --factory-startup --python-exit-code 1 --python stage3_blender.py -- \
  --source "$SOURCE_PATH" \
  --intervals "$INTERVALS_JSON" \
  --output "$BLEND_OUTPUT"

if [[ "${CLEANUP_COPY:-false}" == true ]]; then
  rm -f "${INPUT_VIDEOS_DIR}/$(basename "$SOURCE_PATH")"
fi

echo "Done: $BLEND_OUTPUT"
