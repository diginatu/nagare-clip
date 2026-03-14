#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: ./run_pipeline.sh input/myvideo.mp4 ja"
  exit 1
fi

SOURCE_PATH="$1"
LANGUAGE="$2"
SILENCE_THRESHOLD="${3:-1.5}"
MIN_KEEP="${4:-1.0}"

if [[ ! -f "$SOURCE_PATH" ]]; then
  echo "Source file not found: $SOURCE_PATH"
  exit 1
fi

if [[ "$SOURCE_PATH" != input/* ]]; then
  echo "Source file must be inside input/ so Docker can access it: $SOURCE_PATH"
  exit 1
fi

mkdir -p input output cache config

SOURCE_RELATIVE="${SOURCE_PATH#input/}"
BASENAME="$(basename "$SOURCE_PATH")"
STEM="${BASENAME%.*}"

WHISPER_JSON="output/${STEM}.json"
INTERVALS_JSON="output/${STEM}_intervals.json"
BLEND_OUTPUT="output/${STEM}_edited.blend"

echo "[Stage 1/3] WhisperX transcription"
docker compose run --rm --user "0:0" whisperx \
  _ \
  "$SOURCE_RELATIVE" \
  --output_dir /output \
  --output_format all \
  --language "$LANGUAGE" \
  --compute_type float16 \
  --batch_size 16

echo "[Stage 2/3] Keep interval computation"
python stage2_intervals.py \
  --json "$WHISPER_JSON" \
  --config config/filler_words.yaml \
  --language "$LANGUAGE" \
  --silence_threshold "$SILENCE_THRESHOLD" \
  --min_keep "$MIN_KEEP" \
  --output "$INTERVALS_JSON"

echo "[Stage 3/3] Blender VSE project generation"
blender --background --factory-startup --python-exit-code 1 --python stage3_blender.py -- \
  --source "$SOURCE_PATH" \
  --intervals "$INTERVALS_JSON" \
  --output "$BLEND_OUTPUT"

echo "Done: $BLEND_OUTPUT"
