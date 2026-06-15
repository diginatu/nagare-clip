#!/usr/bin/env bash

set -euo pipefail

# Resolve the project root directory (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  echo "Usage: ./scripts/run_pipeline.sh [OPTIONS]"
  echo ""
  echo "Options:"
  echo "  --source            FILE  Source video file (may be repeated; default: all videos in input-videos-dir)"
  echo "  --config            FILE  Path to YAML config file"
  echo "  --language          LANG  Language code for WhisperX (default: ja)"
  echo "  --input-videos-dir  DIR   Directory containing source videos (default: src_video)"
  echo "  --output-dir        DIR   Root output directory; stage outputs go to named subdirs (default: output)"
  echo "  --keep-pre-margin   SEC   Seconds to extend keep intervals before start (default: 1.0)"
  echo "  --keep-post-margin  SEC   Seconds to extend keep intervals after end (default: 1.0)"
  echo "  --from-stage        S     Start from stage S; reuses earlier stage outputs."
  echo "                            Accepts a stage NAME: transcription, audio_silence,"
  echo "                            text_filter, director, guided_edit, intervals, blender."
  echo "                            Legacy numbers 1-5 are still accepted (1=transcription,"
  echo "                            2=audio_silence, 3=text_filter, 4=intervals, 5=blender)."
  echo "  --to-stage          S     Stop after stage S (inclusive); later stages are skipped."
  echo "                            Same NAME/legacy-number values as --from-stage."
  echo "                            Must not precede --from-stage (default: blender)."
  echo "  --align-model       MODEL HuggingFace model ID for WhisperX alignment"
  echo "                            Japanese default: vumichien/wav2vec2-large-xlsr-japanese"
  echo "                            English default: (whisperx built-in)"
}

CONFIG_FILE=""
INPUT_VIDEOS_DIR=""
OUTPUT_DIR=""
KEEP_PRE_MARGIN=""
KEEP_POST_MARGIN=""
ALIGN_MODEL=""

# Track which values were explicitly set on CLI
CLI_INPUT_VIDEOS_DIR=""
CLI_OUTPUT_DIR=""
CLI_KEEP_PRE_MARGIN=""
CLI_KEEP_POST_MARGIN=""
CLI_ALIGN_MODEL=""
CLI_LANGUAGE=""
CLI_SILENCE_THRESHOLD=""
CLI_MIN_KEEP=""
CLI_FROM_STAGE=""
CLI_TO_STAGE=""
CLI_SOURCES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) CLI_SOURCES+=("$2"); shift 2 ;;
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --from-stage) CLI_FROM_STAGE="$2"; shift 2 ;;
    --to-stage) CLI_TO_STAGE="$2"; shift 2 ;;
    --input-videos-dir) CLI_INPUT_VIDEOS_DIR="$2"; shift 2 ;;
    --output-dir) CLI_OUTPUT_DIR="$2"; shift 2 ;;
    --keep-pre-margin) CLI_KEEP_PRE_MARGIN="$2"; shift 2 ;;
    --keep-post-margin) CLI_KEEP_POST_MARGIN="$2"; shift 2 ;;
    --align-model) CLI_ALIGN_MODEL="$2"; shift 2 ;;
    --language) CLI_LANGUAGE="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    *) break ;;
  esac
done

# --- Resolve config file values for pipeline/stage1 settings ---
CFG_INPUT_VIDEOS_DIR=""
CFG_OUTPUT_DIR=""
CFG_KEEP_PRE_MARGIN=""
CFG_KEEP_POST_MARGIN=""
CFG_ALIGN_MODEL=""
CFG_LANGUAGE=""
CFG_SILENCE_THRESHOLD=""
CFG_MIN_KEEP=""
CFG_FROM_STAGE=""
CFG_TO_STAGE=""
CFG_COMPUTE_TYPE=""
CFG_BATCH_SIZE=""
CFG_USE_LLM=""
CFG_AUDIO_SILENCE_ENABLED=""
CFG_AUDIO_SILENCE_NOISE=""
CFG_AUDIO_SILENCE_MIN_SILENCE=""

if [[ -n "$CONFIG_FILE" ]]; then
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Config file not found: $CONFIG_FILE" >&2
    exit 1
  fi
  eval "$(uv run --project "$PROJECT_ROOT" python3 -c "
import yaml, sys, shlex
with open(sys.argv[1]) as f:
    c = yaml.safe_load(f) or {}
s1 = c.get('transcription', {})
s2 = c.get('text_filter', {})
s3 = c.get('intervals', {})
asl = c.get('audio_silence', {})
p  = c.get('pipeline', {})
def out(name, val):
    if val is not None and val != '':
        print(f'{name}={shlex.quote(str(val))}')
out('CFG_COMPUTE_TYPE', s1.get('compute_type'))
out('CFG_BATCH_SIZE', s1.get('batch_size'))
out('CFG_ALIGN_MODEL', s1.get('align_model'))
out('CFG_LANGUAGE', s1.get('language'))
out('CFG_SILENCE_THRESHOLD', s3.get('silence_threshold'))
out('CFG_MIN_KEEP', s3.get('min_keep'))
out('CFG_KEEP_PRE_MARGIN', s3.get('keep_pre_margin'))
out('CFG_KEEP_POST_MARGIN', s3.get('keep_post_margin'))
out('CFG_USE_LLM', str(bool(s2.get('use_llm', False))).lower())
if 'enabled' in asl:
    out('CFG_AUDIO_SILENCE_ENABLED', str(bool(asl.get('enabled'))).lower())
out('CFG_AUDIO_SILENCE_NOISE', asl.get('noise'))
out('CFG_AUDIO_SILENCE_MIN_SILENCE', asl.get('min_silence'))
out('CFG_INPUT_VIDEOS_DIR', p.get('input_videos_dir'))
out('CFG_OUTPUT_DIR', p.get('output_dir'))
out('CFG_FROM_STAGE', p.get('from_stage'))
out('CFG_TO_STAGE', p.get('to_stage'))
" "$CONFIG_FILE")"
fi

# Precedence: CLI > config > defaults
LANGUAGE="${CLI_LANGUAGE:-${CFG_LANGUAGE:-ja}}"
INPUT_VIDEOS_DIR="${CLI_INPUT_VIDEOS_DIR:-${CFG_INPUT_VIDEOS_DIR:-src_video}}"
OUTPUT_DIR="${CLI_OUTPUT_DIR:-${CFG_OUTPUT_DIR:-output}}"
KEEP_PRE_MARGIN="${CLI_KEEP_PRE_MARGIN:-${CFG_KEEP_PRE_MARGIN:-1.0}}"
KEEP_POST_MARGIN="${CLI_KEEP_POST_MARGIN:-${CFG_KEEP_POST_MARGIN:-1.0}}"
ALIGN_MODEL="${CLI_ALIGN_MODEL:-${CFG_ALIGN_MODEL:-}}"
SILENCE_THRESHOLD="${CLI_SILENCE_THRESHOLD:-${CFG_SILENCE_THRESHOLD:-1.5}}"
MIN_KEEP="${CLI_MIN_KEEP:-${CFG_MIN_KEEP:-1.0}}"
COMPUTE_TYPE="${CFG_COMPUTE_TYPE:-float16}"
BATCH_SIZE="${CFG_BATCH_SIZE:-16}"
FROM_STAGE="${CLI_FROM_STAGE:-${CFG_FROM_STAGE:-1}}"
TO_STAGE="${CLI_TO_STAGE:-${CFG_TO_STAGE:-blender}}"
AUDIO_SILENCE_ENABLED="${CFG_AUDIO_SILENCE_ENABLED:-true}"
AUDIO_SILENCE_NOISE="${CFG_AUDIO_SILENCE_NOISE:--30.0}"
AUDIO_SILENCE_MIN_SILENCE="${CFG_AUDIO_SILENCE_MIN_SILENCE:-0.8}"

# Canonical stage execution order (names are the identifiers; numbers are
# being phased out). New stages are inserted by name, so existing stages never
# need renumbering.
STAGE_ORDER=(transcription audio_silence text_filter summary plan director guided_edit intervals blender)

stage_index() {  # echo 1-based index of a stage name, or nothing
  local name="$1" i
  for i in "${!STAGE_ORDER[@]}"; do
    [[ "${STAGE_ORDER[$i]}" == "$name" ]] && { echo $((i + 1)); return 0; }
  done
  # Not found: echo nothing but still succeed, so callers using
  # `VAR="$(stage_index x)"` under `set -e` reach their own -z validation
  # instead of aborting silently on the command-substitution failure.
  return 0
}

# Resolve --from-stage (a stage name, or a legacy 1-5 number) to an order index.
case "$FROM_STAGE" in
  1) FROM_ORDER="$(stage_index transcription)" ;;
  2) FROM_ORDER="$(stage_index audio_silence)" ;;
  3) FROM_ORDER="$(stage_index text_filter)" ;;
  4) FROM_ORDER="$(stage_index intervals)" ;;
  5) FROM_ORDER="$(stage_index blender)" ;;
  *) FROM_ORDER="$(stage_index "$FROM_STAGE")" ;;
esac
if [[ -z "$FROM_ORDER" ]]; then
  echo "Invalid --from-stage value: $FROM_STAGE" >&2
  echo "Use a stage name (${STAGE_ORDER[*]}) or a legacy number 1-5." >&2
  exit 1
fi

# Resolve --to-stage (a stage name, or a legacy 1-5 number) to an order index.
# Default is the last stage (blender), i.e. no upper bound.
case "$TO_STAGE" in
  1) TO_ORDER="$(stage_index transcription)" ;;
  2) TO_ORDER="$(stage_index audio_silence)" ;;
  3) TO_ORDER="$(stage_index text_filter)" ;;
  4) TO_ORDER="$(stage_index intervals)" ;;
  5) TO_ORDER="$(stage_index blender)" ;;
  *) TO_ORDER="$(stage_index "$TO_STAGE")" ;;
esac
if [[ -z "$TO_ORDER" ]]; then
  echo "Invalid --to-stage value: $TO_STAGE" >&2
  echo "Use a stage name (${STAGE_ORDER[*]}) or a legacy number 1-5." >&2
  exit 1
fi
if (( FROM_ORDER > TO_ORDER )); then
  echo "Invalid stage range: --from-stage ($FROM_STAGE) is after --to-stage ($TO_STAGE)." >&2
  exit 1
fi

# A stage runs only inside the [from, to] window; stages past the window are
# skipped without reusing/validating outputs (they are intentionally not built).
in_window() { (( FROM_ORDER <= $1 && $1 <= TO_ORDER )); }
past_window() { (( $1 > TO_ORDER )); }

ORD_TRANSCRIPTION="$(stage_index transcription)"
ORD_AUDIO_SILENCE="$(stage_index audio_silence)"
ORD_TEXT_FILTER="$(stage_index text_filter)"
ORD_SUMMARY="$(stage_index summary)"
ORD_PLAN="$(stage_index plan)"
ORD_DIRECTOR="$(stage_index director)"
ORD_GUIDED_EDIT="$(stage_index guided_edit)"
ORD_INTERVALS="$(stage_index intervals)"
ORD_BLENDER="$(stage_index blender)"

# Set default alignment model per language if not specified
if [[ -z "$ALIGN_MODEL" ]]; then
  case "$LANGUAGE" in
    ja) ALIGN_MODEL="vumichien/wav2vec2-large-xlsr-japanese" ;;
  esac
fi

STAGE1_DIR="${OUTPUT_DIR}/stage1"          # WhisperX transcription
STAGE2_DIR="${OUTPUT_DIR}/stage2"          # Audio-silence cut lists
STAGE3_DIR="${OUTPUT_DIR}/stage3"          # Text editing checkpoint (_edits.txt)
SUMMARY_DIR="${OUTPUT_DIR}/summary"        # Project-wide summaries (summary.json)
PLAN_DIR="${OUTPUT_DIR}/plan"              # Cross-video rough directions (plan.json)
DIRECTOR_DIR="${OUTPUT_DIR}/director"      # director ops (_director.json)
GUIDED_DIR="${OUTPUT_DIR}/guided_edit"     # augmented _edits.txt + _unapplied.txt
STAGE4_DIR="${OUTPUT_DIR}/stage4"          # Patch + keep-interval merge (_intervals.json)
STAGE5_DIR="${OUTPUT_DIR}/stage5"          # Blender VSE project (.blend)
LLM_REPORT_DIR="${OUTPUT_DIR}/llm_report"  # Per-call LLM report (index.md + detail files)
LOG_FILE="${OUTPUT_DIR}/pipeline.log"

mkdir -p "$INPUT_VIDEOS_DIR" "$STAGE1_DIR" "$STAGE2_DIR" "$STAGE3_DIR" \
  "$SUMMARY_DIR" "$PLAN_DIR" "$DIRECTOR_DIR" "$GUIDED_DIR" "$STAGE4_DIR" \
  "$STAGE5_DIR" "$PROJECT_ROOT/cache"

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

# Build Stage 3 CLI override args (only explicitly-set values)
STAGE3_OVERRIDE_ARGS=()
if [[ -n "$CLI_SILENCE_THRESHOLD" ]]; then
  STAGE3_OVERRIDE_ARGS+=("--silence_threshold" "$CLI_SILENCE_THRESHOLD")
fi
if [[ -n "$CLI_MIN_KEEP" ]]; then
  STAGE3_OVERRIDE_ARGS+=("--min_keep" "$CLI_MIN_KEEP")
fi
if [[ -n "$CLI_KEEP_PRE_MARGIN" ]]; then
  STAGE3_OVERRIDE_ARGS+=("--keep_pre_margin" "$CLI_KEEP_PRE_MARGIN")
fi
if [[ -n "$CLI_KEEP_POST_MARGIN" ]]; then
  STAGE3_OVERRIDE_ARGS+=("--keep_post_margin" "$CLI_KEEP_POST_MARGIN")
fi

# Build align model args
ALIGN_MODEL_ARGS=()
if [[ -n "$ALIGN_MODEL" ]]; then
  ALIGN_MODEL_ARGS=("--align_model" "$ALIGN_MODEL")
fi

# --- Collect per-source metadata and stage any out-of-dir files ---
ALL_SOURCE_PATHS=()
ALL_INTERVALS=()
CLEANUP_COPIES=()
FIRST_STEM=""
ALL_STEMS=()
ALL_RELATIVES=()

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

  ALL_SOURCE_PATHS+=("$ABS_SOURCE")
  ALL_STEMS+=("$STEM")
  ALL_RELATIVES+=("$SOURCE_RELATIVE")
done

# --- Stage 1: WhisperX transcription (single container run for all sources) ---
if in_window "$ORD_TRANSCRIPTION"; then
  echo "[transcription] WhisperX: ${ALL_RELATIVES[*]}"
  INPUT_VIDEOS_DIR="$ABS_INPUT_VIDEOS" OUTPUT_DIR="$ABS_OUTPUT_DIR" \
  docker compose -f "$PROJECT_ROOT/docker-compose.yml" run --rm --user "0:0" whisperx \
    _ \
    "${ALL_RELATIVES[@]}" \
    --output_dir /output/stage1 \
    --output_format all \
    --language "$LANGUAGE" \
    --compute_type "$COMPUTE_TYPE" \
    --batch_size "$BATCH_SIZE" \
    "${ALIGN_MODEL_ARGS[@]}"
elif past_window "$ORD_TRANSCRIPTION"; then
  echo "[transcription] Skipped (--to-stage $TO_STAGE)"
else
  echo "[transcription] Skipped (--from-stage $FROM_STAGE)"
  # Validate that transcription outputs exist for all sources
  for STEM in "${ALL_STEMS[@]}"; do
    if [[ ! -f "${STAGE1_DIR}/${STEM}.json" ]]; then
      echo "Missing transcription output: ${STAGE1_DIR}/${STEM}.json (required when skipping transcription)" >&2
      exit 1
    fi
    if (( FROM_ORDER <= ORD_TEXT_FILTER )) && [[ ! -f "${STAGE1_DIR}/${STEM}.txt" ]]; then
      echo "Missing transcription output: ${STAGE1_DIR}/${STEM}.txt (required for the text editing checkpoint)" >&2
      exit 1
    fi
  done
fi

# --- Stage 2: Audio-silence (jump-cut) detection checkpoint (per source) ---
if in_window "$ORD_AUDIO_SILENCE"; then
  for i in "${!ALL_STEMS[@]}"; do
    STEM="${ALL_STEMS[$i]}"
    CUTS_TXT="${STAGE2_DIR}/${STEM}_cuts.txt"
    RAW_ARGS=()
    echo "[audio_silence] Detection: ${STEM}"
    if [[ "$AUDIO_SILENCE_ENABLED" = "true" ]]; then
      SD_LOG="${STAGE2_DIR}/${STEM}_silencedetect.log"
      INPUT_VIDEOS_DIR="$ABS_INPUT_VIDEOS" OUTPUT_DIR="$ABS_OUTPUT_DIR" \
      docker compose -f "$PROJECT_ROOT/docker-compose.yml" run --rm --user "0:0" \
        --entrypoint ffmpeg whisperx \
        -hide_banner -nostats -i "${ALL_RELATIVES[$i]}" \
        -af "silencedetect=noise=${AUDIO_SILENCE_NOISE}dB:d=${AUDIO_SILENCE_MIN_SILENCE}" \
        -f null - >/dev/null 2> "$SD_LOG"
      RAW_ARGS=(--raw "$SD_LOG")
    fi
    uv run --project "$PROJECT_ROOT" python -m nagare_clip.audio_silence.cli \
      "${RAW_ARGS[@]}" \
      --output "$CUTS_TXT" \
      "${CONFIG_ARGS[@]}" \
      --log-file "$LOG_FILE"
  done
elif past_window "$ORD_AUDIO_SILENCE"; then
  echo "[audio_silence] Skipped (--to-stage $TO_STAGE)"
else
  echo "[audio_silence] Skipped (--from-stage $FROM_STAGE)"
  # Validate that audio_silence outputs exist
  for STEM in "${ALL_STEMS[@]}"; do
    if [[ ! -f "${STAGE2_DIR}/${STEM}_cuts.txt" ]]; then
      echo "Missing audio_silence output: ${STAGE2_DIR}/${STEM}_cuts.txt (required when skipping audio_silence)" >&2
      exit 1
    fi
  done
fi

# --- text_filter: Text editing checkpoint (mandatory, per source) ---
if in_window "$ORD_TEXT_FILTER"; then
  REPORT_CLEARED_TF=0
  for i in "${!ALL_STEMS[@]}"; do
    STEM="${ALL_STEMS[$i]}"
    REPORT_KEEP_TF=()
    if (( REPORT_CLEARED_TF )); then REPORT_KEEP_TF+=(--llm-report-no-clear); fi
    REPORT_CLEARED_TF=1
    echo "[text_filter] Text editing checkpoint: ${STEM}"
    uv run --project "$PROJECT_ROOT" python -m nagare_clip.stage2.cli \
      --txt "${STAGE1_DIR}/${STEM}.txt" \
      --output-txt "${STAGE3_DIR}/${STEM}_edits.txt" \
      "${CONFIG_ARGS[@]}" \
      --log-file "$LOG_FILE" \
      --llm-report-dir "$LLM_REPORT_DIR" \
      "${REPORT_KEEP_TF[@]}"
  done
elif past_window "$ORD_TEXT_FILTER"; then
  echo "[text_filter] Skipped (--to-stage $TO_STAGE)"
else
  echo "[text_filter] Skipped (--from-stage $FROM_STAGE)"
  # Validate that text_filter outputs exist
  for STEM in "${ALL_STEMS[@]}"; do
    if [[ ! -f "${STAGE3_DIR}/${STEM}_edits.txt" ]]; then
      echo "Missing text_filter output: ${STAGE3_DIR}/${STEM}_edits.txt (required when skipping text_filter)" >&2
      exit 1
    fi
  done
fi

# --- summary: project-wide per-part + all-videos summaries (single run) ---
# Always runs (a no-op when summary.enabled is false, writing an empty summary).
if in_window "$ORD_SUMMARY"; then
  echo "[summary] Project-wide summaries"
  EDITS_ARGS=()
  for STEM in "${ALL_STEMS[@]}"; do
    EDITS_ARGS+=(--edits-txt "${STAGE3_DIR}/${STEM}_edits.txt")
    EDITS_ARGS+=(--json "${STAGE1_DIR}/${STEM}.json")
  done
  uv run --project "$PROJECT_ROOT" python -m nagare_clip.summary.cli \
    "${EDITS_ARGS[@]}" \
    --output "${SUMMARY_DIR}/summary.json" \
    "${CONFIG_ARGS[@]}" \
    --log-file "$LOG_FILE" \
    --llm-report-dir "$LLM_REPORT_DIR"
elif past_window "$ORD_SUMMARY"; then
  echo "[summary] Skipped (--to-stage $TO_STAGE)"
else
  echo "[summary] Skipped (--from-stage $FROM_STAGE)"
  if [[ ! -f "${SUMMARY_DIR}/summary.json" ]]; then
    echo "Missing summary output: ${SUMMARY_DIR}/summary.json (required when skipping summary)" >&2
    exit 1
  fi
fi

# --- plan: cross-video rough directions per part (single run) ---
# Always runs (a no-op when plan.enabled is false, writing an empty plan).
if in_window "$ORD_PLAN"; then
  echo "[plan] Cross-video rough directions"
  uv run --project "$PROJECT_ROOT" python -m nagare_clip.plan.cli \
    --summary "${SUMMARY_DIR}/summary.json" \
    --output "${PLAN_DIR}/plan.json" \
    "${CONFIG_ARGS[@]}" \
    --log-file "$LOG_FILE" \
    --llm-report-dir "$LLM_REPORT_DIR"
elif past_window "$ORD_PLAN"; then
  echo "[plan] Skipped (--to-stage $TO_STAGE)"
else
  echo "[plan] Skipped (--from-stage $FROM_STAGE)"
  if [[ ! -f "${PLAN_DIR}/plan.json" ]]; then
    echo "Missing plan output: ${PLAN_DIR}/plan.json (required when skipping plan)" >&2
    exit 1
  fi
fi

# --- director: LLM high-level edit operations (per source) ---
# Always runs (a no-op when director.enabled is false, writing an empty op list).
if in_window "$ORD_DIRECTOR"; then
  REPORT_CLEARED_DIR=0
  for STEM in "${ALL_STEMS[@]}"; do
    REPORT_KEEP_DIR=()
    if (( REPORT_CLEARED_DIR )); then REPORT_KEEP_DIR+=(--llm-report-no-clear); fi
    REPORT_CLEARED_DIR=1
    echo "[director] Edit operations: ${STEM}"
    uv run --project "$PROJECT_ROOT" python -m nagare_clip.director.cli \
      --edits-txt "${STAGE3_DIR}/${STEM}_edits.txt" \
      --output "${DIRECTOR_DIR}/${STEM}_director.json" \
      --summary "${SUMMARY_DIR}/summary.json" \
      --plan "${PLAN_DIR}/plan.json" \
      --stem "${STEM}" \
      --json "${STAGE1_DIR}/${STEM}.json" \
      "${CONFIG_ARGS[@]}" \
      --log-file "$LOG_FILE" \
      --llm-report-dir "$LLM_REPORT_DIR" \
      "${REPORT_KEEP_DIR[@]}"
  done
elif past_window "$ORD_DIRECTOR"; then
  echo "[director] Skipped (--to-stage $TO_STAGE)"
else
  echo "[director] Skipped (--from-stage $FROM_STAGE)"
  for STEM in "${ALL_STEMS[@]}"; do
    if [[ ! -f "${DIRECTOR_DIR}/${STEM}_director.json" ]]; then
      echo "Missing director output: ${DIRECTOR_DIR}/${STEM}_director.json (required when skipping director)" >&2
      exit 1
    fi
  done
fi

# --- guided_edit: apply director ops into _edits.txt (per source) ---
# Always runs (a no-op when guided_edit.enabled is false, copying edits through).
if in_window "$ORD_GUIDED_EDIT"; then
  REPORT_CLEARED_GE=0
  for STEM in "${ALL_STEMS[@]}"; do
    REPORT_KEEP_GE=()
    if (( REPORT_CLEARED_GE )); then REPORT_KEEP_GE+=(--llm-report-no-clear); fi
    REPORT_CLEARED_GE=1
    echo "[guided_edit] Applying director ops: ${STEM}"
    uv run --project "$PROJECT_ROOT" python -m nagare_clip.guided_edit.cli \
      --edits-txt "${STAGE3_DIR}/${STEM}_edits.txt" \
      --director "${DIRECTOR_DIR}/${STEM}_director.json" \
      --output "${GUIDED_DIR}/${STEM}_edits.txt" \
      --unapplied "${GUIDED_DIR}/${STEM}_unapplied.txt" \
      --json "${STAGE1_DIR}/${STEM}.json" \
      "${CONFIG_ARGS[@]}" \
      --log-file "$LOG_FILE" \
      --llm-report-dir "$LLM_REPORT_DIR" \
      "${REPORT_KEEP_GE[@]}"
  done
elif past_window "$ORD_GUIDED_EDIT"; then
  echo "[guided_edit] Skipped (--to-stage $TO_STAGE)"
else
  echo "[guided_edit] Skipped (--from-stage $FROM_STAGE)"
  for STEM in "${ALL_STEMS[@]}"; do
    if [[ ! -f "${GUIDED_DIR}/${STEM}_edits.txt" ]]; then
      echo "Missing guided_edit output: ${GUIDED_DIR}/${STEM}_edits.txt (required when skipping guided_edit)" >&2
      exit 1
    fi
  done
fi

# --- intervals: Patch application + keep interval computation (per source) ---
# Reads the guided_edit output (which is the text_filter edits passed through
# when guided_edit is disabled).
if in_window "$ORD_INTERVALS"; then
  for i in "${!ALL_STEMS[@]}"; do
    STEM="${ALL_STEMS[$i]}"
    INTERVALS_JSON="${STAGE4_DIR}/${STEM}_intervals.json"

    echo "[intervals] Patch application + keep intervals: ${STEM}"
    uv run --project "$PROJECT_ROOT" python -m nagare_clip.cli \
      --edits-txt "${GUIDED_DIR}/${STEM}_edits.txt" \
      --json "${STAGE1_DIR}/${STEM}.json" \
      --cuts-txt "${STAGE2_DIR}/${STEM}_cuts.txt" \
      "${CONFIG_ARGS[@]}" \
      "${STAGE3_OVERRIDE_ARGS[@]}" \
      --output "$INTERVALS_JSON" \
      --log-file "$LOG_FILE"

    ALL_INTERVALS+=("$(realpath "$INTERVALS_JSON")")
  done
elif past_window "$ORD_INTERVALS"; then
  echo "[intervals] Skipped (--to-stage $TO_STAGE)"
else
  echo "[intervals] Skipped (--from-stage $FROM_STAGE)"
  # Validate that intervals outputs exist and collect interval paths
  for STEM in "${ALL_STEMS[@]}"; do
    INTERVALS_JSON="${STAGE4_DIR}/${STEM}_intervals.json"
    if [[ ! -f "$INTERVALS_JSON" ]]; then
      echo "Missing intervals output: $INTERVALS_JSON (required when skipping intervals)" >&2
      exit 1
    fi
    ALL_INTERVALS+=("$(realpath "$INTERVALS_JSON")")
  done
fi

# --- Stage 5: Blender VSE project generation ---
BLEND_OUTPUT="${STAGE5_DIR}/${FIRST_STEM}_edited.blend"

STAGE4_SOURCE_ARGS=()
for src in "${ALL_SOURCE_PATHS[@]}"; do
  STAGE4_SOURCE_ARGS+=("--source" "$src")
done

STAGE4_INTERVALS_ARGS=()
for ivp in "${ALL_INTERVALS[@]}"; do
  STAGE4_INTERVALS_ARGS+=("--intervals" "$ivp")
done

if in_window "$ORD_BLENDER"; then
  echo "[blender] VSE project generation"
  blender --background --factory-startup --python-exit-code 1 --python "$PROJECT_ROOT/src/nagare_clip/stage4/blender_cli.py" -- \
    "${STAGE4_SOURCE_ARGS[@]}" \
    "${STAGE4_INTERVALS_ARGS[@]}" \
    --output "$BLEND_OUTPUT" \
    "${CONFIG_ARGS[@]}" \
    --log-file "$LOG_FILE"
else
  echo "[blender] Skipped (--to-stage $TO_STAGE)"
fi

# Cleanup any copied source files
for f in "${CLEANUP_COPIES[@]}"; do
  rm -f "$f"
done

if in_window "$ORD_BLENDER"; then
  echo "Done: $BLEND_OUTPUT"
else
  echo "Done (stopped at --to-stage $TO_STAGE)"
fi
