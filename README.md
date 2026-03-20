# video-editor-ai

Semi-automated video editing pipeline for long-form recordings on Linux.

The pipeline creates a rough-cut Blender project for human review and fine-tuning.

## Pipeline Stages

1. Stage 1: WhisperX in Docker -> transcript outputs (`json`, `srt`, `vtt`, etc.)
2. Stage 2: Python interval logic -> `*_intervals.json` keep ranges
3. Stage 3: Blender headless -> `.blend` with VSE strips arranged back-to-back

## Requirements

- Linux
- NVIDIA GPU + NVIDIA Container Toolkit
- Docker + Docker Compose
- Blender available as `blender`
- Python 3.11+

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv sync          # install runtime deps
uv sync --dev    # install runtime + dev deps (includes pytest)
```

Run tests:

```bash
uv run pytest
```

## Quick Start

1. Put source media in `src_video/` (default input directory).
2. Run the full pipeline (defaults resolve to `src_video/` and `output/`):

```bash
./scripts/run_pipeline.sh "myvideo.mp4" ja
```

Pass custom locations with options:

```bash
./scripts/run_pipeline.sh --input-videos-dir my_videos --output-dir my_out "myvideo.mp4" ja
```

Use a YAML config file to tune pipeline parameters (see `config.example.yml`):

```bash
./scripts/run_pipeline.sh --config my_project.yml "myvideo.mp4" ja
```

Override the alignment model (e.g. to revert to the WhisperX built-in default for Japanese):

```bash
./scripts/run_pipeline.sh --align-model jonatasgrosman/wav2vec2-large-xlsr-53-japanese "myvideo.mp4" ja
```

This produces outputs under `output/` (or your `--output-dir`), including:

- `myvideo.json`
- `myvideo.srt`
- `myvideo.vtt`
- `myvideo_intervals.json`
- `myvideo_edited.blend`

## Configuration

All pipeline parameters can be controlled via a YAML config file. Copy `config.example.yml` as a starting point:

```bash
cp config.example.yml my_project.yml
# edit my_project.yml as needed
./scripts/run_pipeline.sh --config my_project.yml "myvideo.mp4" ja
```

Parameters resolve in this priority order (highest wins):

1. CLI flags (e.g. `--pre-margin 2.0`)
2. Config file values
3. Built-in defaults

The config file covers all sections (`general`, `stage1`, `stage2`, `stage3`, `pipeline`). See `config.example.yml` for the full list of keys and their defaults.

## CLI

```bash
./scripts/run_pipeline.sh [--config FILE] [--input-videos-dir DIR] [--output-dir DIR] [--pre-margin SEC] [--post-margin SEC] [--align-model MODEL] <source> <language> [silence_threshold] [min_keep]
```

- `--config FILE` — path to a YAML config file; config values fill in between CLI overrides and built-in defaults.
- Defaults: input videos under `src_video/`, outputs under `output/`.
- If `<source>` contains `/`, it is treated as the exact path; otherwise it is resolved inside `--input-videos-dir`.
- `silence_threshold` and `min_keep` default to `1.5` and `1.0` (overridable via config).
- `pre-margin`/`post-margin` extend keep intervals before/after by default `1.0s` and merge overlaps.
- `--align-model` overrides the HuggingFace model used for WhisperX forced alignment. For Japanese (`ja`), defaults to `vumichien/wav2vec2-large-xlsr-japanese` which showed better alignment scores than the WhisperX built-in default (`jonatasgrosman/wav2vec2-large-xlsr-53-japanese`); for other languages the WhisperX built-in model is used.

## Stage Commands

### Stage 1 only (WhisperX)

```bash
docker compose run --rm --user "0:0" whisperx \
  _ \
  "myvideo.mp4" \
  --output_dir /output \
  --output_format all \
  --language ja \
  --compute_type float16 \
  --batch_size 16
```

Notes:

- Input files are mounted to `/app` via `${INPUT_VIDEOS_DIR:-src_video}:/app` (set env vars or rely on defaults).
- Output files are mounted to `/output` via `${OUTPUT_DIR:-output}:/output`.
- This image tag does not accept `--word_timestamps`.
- No diarization flags are used.

### Stage 2 only (interval generation)

```bash
uv run python -m video_editor_ai.cli \
  --json output/myvideo.json \
  --config my_project.yml \
  --output output/myvideo_intervals.json
```

CLI flags override config file values:

```bash
uv run python -m video_editor_ai.cli \
  --json output/myvideo.json \
  --silence_threshold 1.5 \
  --min_keep 1.0 \
  --pre_margin 1.0 \
  --post_margin 1.0 \
  --caption_max_bunsetu 12 \
  --caption_min_bunsetu 3 \
  --caption_max_duration 4.0 \
  --caption_min_duration 1.5 \
  --caption_silence_flush 1.5 \
  --output output/myvideo_intervals.json

Keep-interval silence detection uses WhisperX word timings (`word.start`/`word.end`) with a per-word max-span cap (0.6s) so inflated token ends do not mask real pauses. Bunsetsu timing uses `ginza.bunsetu_spans(doc)` (GiNZA/spaCy) so particles and auxiliaries are attached to the preceding content word, producing natural subtitle line-break units. It detects large intra-bunsetsu character gaps (> 0.6s) caused by WhisperX misalignment and snaps the bunsetsu start forward to the later character cluster so silence is not hidden inside a single bunsetsu. Caption chunks use bunsetsu-level timing (`end = min(start+0.02s, next_bunsetu_start)`) and are split on detected silence gaps and keep-boundary crossings. Captions are preserved as transcript chunks and Stage 2 expands keep intervals to include caption spans so subtitle text is not dropped at Stage 3, then re-applies minimum keep duration (`--min_keep`) to avoid tiny strips. Tune chunking with `--caption_max_bunsetu`, `--caption_min_bunsetu`, `--caption_max_duration`, `--caption_min_duration`, and `--caption_silence_flush`.
```

### Stage 3 only (Blender VSE project)

```bash
blender --background --factory-startup --python-exit-code 1 --python src/video_editor_ai/stage3/blender_cli.py -- \
  --source src_video/myvideo.mp4 \
  --intervals output/myvideo_intervals.json \
  --output output/myvideo_edited.blend \
  --config my_project.yml
```

## Operational Notes

- `scripts/run_pipeline.sh` currently runs WhisperX as root (`--user "0:0"`) for compatibility with this image/runtime.
- As a result, Stage 1 output files can be root-owned on host.
- If needed, fix ownership after run:

```bash
sudo chown -R "$(id -u):$(id -g)" output cache
```
