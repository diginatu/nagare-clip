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

## Repository Layout

```text
.
├── cache/
├── config/
│   └── filler_words.yaml
├── src_video/
├── output/
├── docker-compose.yml
├── run_pipeline.sh
├── stage2_intervals.py
└── stage3_blender.py
```

## Quick Start

1. Put source media in `src_video/` (default input directory).
2. Run the full pipeline (defaults resolve to `src_video/` and `output/`):

```bash
./run_pipeline.sh "myvideo.mp4" ja
```

Pass custom locations with options:

```bash
./run_pipeline.sh --input-videos-dir my_videos --output-dir my_out "myvideo.mp4" ja
```

Override the alignment model (e.g. to revert to the WhisperX built-in default for Japanese):

```bash
./run_pipeline.sh --align-model jonatasgrosman/wav2vec2-large-xlsr-53-japanese "myvideo.mp4" ja
```

This produces outputs under `output/` (or your `--output-dir`), including:

- `myvideo.json`
- `myvideo.srt`
- `myvideo.vtt`
- `myvideo_intervals.json`
- `myvideo_edited.blend`

## CLI

```bash
./run_pipeline.sh [--input-videos-dir DIR] [--output-dir DIR] [--pre-margin SEC] [--post-margin SEC] [--align-model MODEL] <source> <language> [silence_threshold] [min_keep]
```

- Defaults: input videos under `src_video/`, outputs under `output/`.
- If `<source>` contains `/`, it is treated as the exact path; otherwise it is resolved inside `--input-videos-dir`.
- `silence_threshold` and `min_keep` keep their existing defaults of `1.5` and `1.0`.
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
python stage2_intervals.py \
  --json output/myvideo.json \
  --config config/filler_words.yaml \
  --language ja \
  --silence_threshold 1.5 \
  --min_keep 1.0 \
  --pre_margin 1.0 \
  --post_margin 1.0 \
  --caption_max_morphemes 12 \
  --caption_min_morphemes 3 \
  --caption_max_duration 4.0 \
  --caption_min_duration 1.5 \
  --caption_silence_flush 1.5 \
  --output output/myvideo_intervals.json

Caption chunks use `fugashi` morphological segmentation with morpheme-level timing (`end = min(start+0.02s, next_morpheme_start)`) to avoid false silence gaps. Tune size and timing with `--caption_max_morphemes`, `--caption_min_morphemes`, `--caption_max_duration`, `--caption_min_duration`, and `--caption_silence_flush`.
```

### Stage 3 only (Blender VSE project)

```bash
blender --background --factory-startup --python-exit-code 1 --python stage3_blender.py -- \
  --source src_video/myvideo.mp4 \
  --intervals output/myvideo_intervals.json \
  --output output/myvideo_edited.blend
```

## Config

`config/filler_words.yaml` contains language-specific filler terms used by Stage 2.

Example:

```yaml
ja:
  - えーと
  - あのー
  - うーん
  - えっと
  - まあ
en:
  - um
  - uh
  - like
  - you know
```

## Operational Notes

- `run_pipeline.sh` currently runs WhisperX as root (`--user "0:0"`) for compatibility with this image/runtime.
- As a result, Stage 1 output files can be root-owned on host.
- If needed, fix ownership after run:

```bash
sudo chown -R "$(id -u):$(id -g)" output cache
```
