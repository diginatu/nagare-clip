# nagare-clip

Semi-automated video editing pipeline for long-form recordings on Linux.

The pipeline creates a rough-cut Blender project for human review and fine-tuning.

## Pipeline Stages

1. Stage 1: WhisperX in Docker -> transcript outputs (`json`, `srt`, `vtt`, etc.)
2. Stage 2: Audio-silence (jump-cut) detection -> `_cuts.txt` editable cut list
3. Stage 3: Text editing checkpoint -> `_edits.txt` (copy of `.txt`, or LLM-corrected with `{{old->new}}` markers)
4. Stage 4: Patch application + keep intervals -> `*_intervals.json` keep ranges (audio cuts unioned in)
5. Stage 5: Blender headless -> `.blend` with VSE strips arranged back-to-back

## Human Editing Workflow

1. Run stages 1–3: `./scripts/run_pipeline.sh`
2. Edit `output/stage2/{stem}_cuts.txt` — delete a line to keep that span, or adjust the `START - END` times
3. Edit `output/stage3/{stem}_edits.txt` — add/modify `{{old->new}}` patch markers, or wrap text in `<keep>...</keep>` to force-keep its audio
4. Resume: `./scripts/run_pipeline.sh --from-stage 4 --source myvideo.mp4`

The `{{old->new}}` syntax replaces `old` with `new` in the transcript. Use `{{delete->}}` to remove text, `{{->insert}}` to insert text.

The `<keep>...</keep>` tag preserves the audio under the wrapped text — Stage 4 carves that time range out of both the word-gap silence detection and any overlapping `_cuts.txt` ranges, so dramatic pauses and intentional silences survive. The tag may be opened on one line and closed on a later one, so a single `<keep>` block can span multiple lines and preserve the silences between them. The tag is added by the human (not the LLM) after `_edits.txt` is produced.

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
2. Run the full pipeline — processes all videos in `src_video/` alphabetically:

```bash
./scripts/run_pipeline.sh
```

Or target a single file with `--source`:

```bash
./scripts/run_pipeline.sh --source myvideo.mp4
```

Pass custom locations with options:

```bash
./scripts/run_pipeline.sh --input-videos-dir my_videos --output-dir my_out
```

Use a YAML config file to tune pipeline parameters (see `config.example.yml`):

```bash
./scripts/run_pipeline.sh --config my_project.yml
```

Override the language (default is `ja`):

```bash
./scripts/run_pipeline.sh --language en
```

Re-run from a specific stage (skip expensive earlier stages when iterating on config):

```bash
# Skip Stage 1, reuse WhisperX output, re-run silence detection + edits + intervals + Blender
./scripts/run_pipeline.sh --from-stage 2 --source myvideo.mp4

# Skip Stage 1–3, apply silence cuts + text edits and regenerate intervals + Blender project
./scripts/run_pipeline.sh --from-stage 4 --source myvideo.mp4

# Skip Stage 1–4, only regenerate the Blender project
./scripts/run_pipeline.sh --from-stage 5 --source myvideo.mp4
```

Override the alignment model (e.g. to revert to the WhisperX built-in default for Japanese):

```bash
./scripts/run_pipeline.sh --align-model jonatasgrosman/wav2vec2-large-xlsr-53-japanese
```

This produces outputs under `output/` (or your `--output-dir`), including:

- `stage1/myvideo.json`, `stage1/myvideo.txt`
- `stage2/myvideo_cuts.txt`
- `stage3/myvideo_edits.txt`
- `stage4/myvideo_intervals.json`
- `stage5/myvideo_edited.blend` (named after the first source file)

## Configuration

All pipeline parameters can be controlled via a YAML config file. Copy `config.example.yml` as a starting point:

```bash
cp config.example.yml my_project.yml
# edit my_project.yml as needed
./scripts/run_pipeline.sh --config my_project.yml
```

Parameters resolve in this priority order (highest wins):

1. CLI flags (e.g. `--pre-margin 2.0`)
2. Config file values
3. Built-in defaults

The config file covers all sections (`general`, `stage1`, `audio_silence`, `stage2`, `stage3`, `stage4`, `pipeline`). The config section names are functional labels, not pipeline-stage numbers: `audio_silence` drives pipeline Stage 2, `stage2.*` drives the Stage 3 text-editing checkpoint, and `stage3.*` drives the Stage 4 interval merge. See `config.example.yml` for the full list of keys and their defaults.

### Stage 2: Audio-Silence Detection

Pipeline Stage 2 runs ffmpeg `silencedetect` (inside the whisperx Docker image) on the waveform and writes an editable `{stem}_cuts.txt` cut list. Each non-comment line is a `START - END` silent span (seconds) that will be cut from the video; delete a line to keep that span, or adjust the times. Stage 4 unions these reviewed ranges into its keep-interval excludes. This is acoustic silence — distinct from `stage3.silence_threshold`, which is a WhisperX word-gap heuristic.

```yaml
audio_silence:
  enabled: true       # false = write an empty cut list (no audio cuts applied)
  noise: -30.0        # ffmpeg silencedetect noise threshold, in dB
  min_silence: 0.8    # minimum silence duration to report, seconds
```

### Stage 3: Text Editing (LLM optional)

The text-editing checkpoint always produces `{stem}_edits.txt`. When `stage2.use_llm` is `false` (default), it copies the Stage 1 `.txt` as-is. When enabled, it runs LLM-based transcription correction and writes output with `{{old->new}}` markers preserved for human review.

```yaml
stage2:
  use_llm: true
  api_base: "http://localhost:11434/v1"
  model: "qwen3.5:4b"
  thinking: "low"   # thinking mode: true/false, or "low"/"medium"/"high" for supported models
```

The LLM uses `{{old->new}}` inline patch syntax to mark corrections. Human editors can then review and modify the markers in `_edits.txt` before Stage 4 applies them.

Human editors can also wrap a span in `<keep>...</keep>` to force-preserve the audio under that text. Stage 4 derives the time range from the first wrapped word's start to the last wrapped word's end and carves it out of both the word-gap silence and any overlapping `_cuts.txt` ranges. The marker may be opened on one line and closed on a later line, so a single `<keep>` block can span multiple WhisperX segments and preserve the silences between them. The marker is added after the LLM filter has produced `_edits.txt`, so the LLM never sees it.

`thinking` sends `"think"` in the Ollama API request, enabling chain-of-thought reasoning for supported models (e.g. qwen3, deepseek-r1). Set `true`/`false`, or a string level like `"low"`, `"medium"`, `"high"` for models that support granular control (e.g. Qwen 3.5). Ollama returns the reasoning trace in a separate field; the pipeline uses only the final answer.

#### Summary LLM (optional)

When `stage2.summary_llm.enabled` is `true`, a separate LLM analyzes the full transcript before filtering to generate a short summary and a list of rare/domain-specific keywords. These are appended to the filter LLM's system prompt so it can better correct mis-dictated words. The summary LLM has its own independent config (model, api_base, temperature, etc.), so you can use a larger model for summarization and a smaller one for filtering.

```yaml
stage2:
  use_llm: true
  summary_llm:
    enabled: true
    model: "gemma3:27b"    # can use a different/larger model
```

Falls back gracefully if the summary LLM call fails — filtering proceeds without the extra context.

## CLI

```bash
./scripts/run_pipeline.sh [OPTIONS]
```

Options:
- `--source FILE` — source video file (may be repeated for multiple sources); when omitted, all videos in `--input-videos-dir` are processed alphabetically.
- `--config FILE` — path to a YAML config file; config values fill in between CLI overrides and built-in defaults.
- `--language LANG` — ISO 639-1 language code passed to WhisperX (default: `ja`). Also settable via `stage1.language` in config.
- `--from-stage N` — start from stage N (1-5); reuses earlier stage outputs. Also settable via `pipeline.from_stage` in config.
- Defaults: input videos under `src_video/`, outputs under `output/`.
- If `--source` contains `/`, it is treated as the exact path; otherwise it is resolved inside `--input-videos-dir`.
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

### Stage 2 only (audio-silence detection)

ffmpeg runs inside the whisperx image; capture its stderr, then parse it:

```bash
INPUT_VIDEOS_DIR=src_video OUTPUT_DIR=output \
docker compose run --rm --user "0:0" --entrypoint ffmpeg whisperx \
  -hide_banner -nostats -i "myvideo.mp4" \
  -af "silencedetect=noise=-30.0dB:d=0.8" -f null - \
  >/dev/null 2> output/stage2/myvideo_silencedetect.log

uv run python -m nagare_clip.audio_silence.cli \
  --raw output/stage2/myvideo_silencedetect.log \
  --output output/stage2/myvideo_cuts.txt \
  --config my_project.yml
```

Omit `--raw` (or set `audio_silence.enabled: false`) to write an empty cut list.

### Stage 4 only (patch application + interval generation)

```bash
uv run python -m nagare_clip.cli \
  --edits-txt output/stage3/myvideo_edits.txt \
  --json output/stage1/myvideo.json \
  --cuts-txt output/stage2/myvideo_cuts.txt \
  --config my_project.yml \
  --output output/stage4/myvideo_intervals.json
```

CLI flags override config file values:

```bash
uv run python -m nagare_clip.cli \
  --edits-txt output/stage3/myvideo_edits.txt \
  --json output/stage1/myvideo.json \
  --cuts-txt output/stage2/myvideo_cuts.txt \
  --silence_threshold 1.5 \
  --min_keep 1.0 \
  --keep_pre_margin 1.0 \
  --keep_post_margin 1.0 \
  --caption_max_bunsetu 12 \
  --caption_min_bunsetu 3 \
  --caption_max_duration 4.0 \
  --caption_min_duration 1.5 \
  --caption_silence_flush 1.5 \
  --output output/stage4/myvideo_intervals.json

Keep-interval silence detection uses WhisperX word timings (`word.start`/`word.end`) with a per-word max-span cap (0.6s) so inflated token ends do not mask real pauses. Bunsetsu timing uses `ginza.bunsetu_spans(doc)` (GiNZA/spaCy) so particles and auxiliaries are attached to the preceding content word, producing natural subtitle line-break units. It detects large intra-bunsetsu character gaps (> 0.6s) caused by WhisperX misalignment and snaps the bunsetsu start forward to the later character cluster so silence is not hidden inside a single bunsetsu. Caption chunks use bunsetsu-level timing (`end = min(start+0.02s, next_bunsetu_start)`) and are split on detected silence gaps and keep-boundary crossings. Captions are preserved as transcript chunks and the interval stage expands keep intervals to include caption spans so subtitle text is not dropped at the Blender stage, then re-applies minimum keep duration (`--min_keep`) to avoid tiny strips. Tune chunking with `--caption_max_bunsetu`, `--caption_min_bunsetu`, `--caption_max_duration`, `--caption_min_duration`, and `--caption_silence_flush`.
```

### Stage 5 only (Blender VSE project)

```bash
blender --background --factory-startup --python-exit-code 1 --python src/nagare_clip/stage4/blender_cli.py -- \
  --source src_video/myvideo.mp4 \
  --intervals output/stage4/myvideo_intervals.json \
  --output output/stage5/myvideo_edited.blend \
  --config my_project.yml
```

## Operational Notes

- `scripts/run_pipeline.sh` currently runs WhisperX as root (`--user "0:0"`) for compatibility with this image/runtime.
- As a result, Stage 1 output files can be root-owned on host.
- If needed, fix ownership after run:

```bash
sudo chown -R "$(id -u):$(id -g)" output cache
```
