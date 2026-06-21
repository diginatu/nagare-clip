# nagare-clip

Semi-automated video editing pipeline for long-form recordings on Linux.

The pipeline creates a rough-cut Blender project for human review and fine-tuning.

## Pipeline Stages

1. transcription: WhisperX in Docker -> transcript outputs (`json`, `srt`, `vtt`, etc.)
2. audio_silence: Audio-silence (jump-cut) detection -> `_cuts.txt` editable cut list
3. text_filter: Text editing checkpoint -> `_edits.txt` (copy of `.txt`, or LLM-corrected with `{{old->new}}` markers)
4. summary (optional, project-wide): a larger LLM segments every video into line-range parts + summaries and writes one all-videos summary -> reviewable `output/summary/summary.json`
5. plan (optional, project-wide): a larger LLM gives a coarse, cross-video rough direction per part -> reviewable `output/plan/plan.json`
6. director (optional): a larger LLM proposes high-level edits -> reviewable `_director.json` op list (fed the summary/plan overview context)
7. guided_edit (optional): a small LLM applies the director's ops into `_edits.txt` (deterministically verified)
8. intervals: Patch application + keep intervals -> `*_intervals.json` keep ranges (audio cuts unioned in)
9. blender: Blender headless -> `.blend` with VSE strips arranged back-to-back

Stages are referenced by name (`--from-stage <name>`); the summary/plan/director/guided_edit stages are no-ops unless enabled in config.

## Human Editing Workflow

1. Run the transcription, audio_silence and text_filter stages: `./scripts/run_pipeline.sh`
2. Edit `output/audio_silence/{stem}_cuts.txt` — delete a line to keep that span, or adjust the `START - END` times
3. Edit `output/text_filter/{stem}_edits.txt` — add/modify `{{old->new}}` patch markers, wrap text in `<keep>...</keep>` to force-keep its audio, wrap text in `<speed factor="N.N">...</speed>` to play that region at the given speed in Blender (does not force-keep audio — nest inside `<keep>` to also keep it), wrap text in `<overlay text="...">...</overlay>` to display an on-screen TEXT strip (does not affect audio), or wrap text in `<cut>...</cut>` to delete it (larger deletions drop out of the timeline via silence detection)
4. Resume: `./scripts/run_pipeline.sh --from-stage intervals --source myvideo.mp4`

The `{{old->new}}` syntax replaces `old` with `new` in the transcript. Use `{{delete->}}` to remove text, `{{->insert}}` to insert text.

The optional `director` + `guided_edit` stages automate steps like the above with LLMs: enable `director.enabled`/`guided_edit.enabled` in config, then a larger LLM proposes edits (cut/speed/overlay/keep/edit) into a reviewable `output/director/{stem}_director.json`, and guided_edit applies them — any op it cannot apply cleanly is logged and recorded in the LLM report (`output/llm_report/`). Span ops (cut/speed/overlay/keep) are a pure whole-line-range wrap, applied deterministically with no LLM call; if a span op's range overlaps a same-type tag already in the file (e.g. one you hand-authored) or an earlier op, it is clipped to the free lines (or dropped if fully covered) so the tags stay valid. Only `edit` ops (a within-line `{{old->new}}` the director only described in prose) call the small LLM, retrying per op on an error / failed verification and nudging the temperature up each attempt. The director likewise retries a failed LLM call (connection error / unparseable JSON); tune `max_retries`, `retry_temp_step`, and `retry_temp_cap` per stage (`max_retries: 0` disables retry).

The optional `summary` + `plan` stages run **once over all source videos** (project-wide) before `director` and give it cross-video context. Enable `summary.enabled`/`plan.enabled` in config: `summary` segments each transcript into line-range parts with a summary each and writes one all-videos summary to `output/summary/summary.json`; `plan` reads those summaries and writes a coarse, cross-video rough direction per part (e.g. "remove — repeats an earlier part", "keep tight") to `output/plan/plan.json`. Both files are human-reviewable/editable. When enabled, the `director` for each video receives the overall summary plus that video's parts (line ranges, summaries, rough directions) and one-line context for the other videos, so its precise per-line ops follow the project-wide plan. They share the same `max_retries`/`retry_temp_step`/`retry_temp_cap` retry knobs.

To help the LLMs reason about pacing, the `plan` and `director` stages now also see **calculated durations and in-between gaps**: the `director`'s numbered transcript annotates each line with `[4.2s, gap 0.8s]` (per-sentence duration + gap to the next line) and the `plan`'s per-part context shows each part's duration and gap. These times come from the WhisperX `{stem}.json`, which the `summary` and `director` stages read via a `--json` flag — the pipeline wires this automatically, so you only need it when running a stage on its own.

Before resuming, you can validate your edits in one pass (reports **every** problem at once, with line numbers, instead of failing on the first like the intervals stage does):

```bash
uv run python -m nagare_clip.intervals.check_edits \
  --edits-txt output/text_filter/myvideo_edits.txt \
  --json output/transcription/myvideo.json
```

It checks line-count vs. JSON segments, `{{old->new}}` patch syntax (empty `{{old->}}` deletions are allowed), decomposition integrity against the original transcript, and `<keep>`/`<speed>`/`<overlay>`/`<cut>` tag balance and well-formedness. Exit code is non-zero when any problem is found.

The `<keep>...</keep>` tag preserves the audio under the wrapped text — the intervals stage carves that time range out of both the word-gap silence detection and any overlapping `_cuts.txt` ranges, so dramatic pauses and intentional silences survive. The tag may be opened on one line and closed on a later one, so a single `<keep>` block can span multiple lines and preserve the silences between them. The tag is added by the human (not the LLM) after `_edits.txt` is produced.

The `<speed factor="N.N">...</speed>` tag instructs the blender stage to play the wrapped region at the given playback speed (e.g., `factor="2.0"` for 2× fast-forward, `factor="0.5"` for slow-motion) via a Blender VSE Speed Control effect strip. Unlike `<keep>`, **it does not force-keep audio** — silence inside a `<speed>` span is still cut by silence detection, and the speed only applies to the surviving spoken parts. To preserve the audio **and** speed it up, nest the tags: `<keep><speed factor="2.0">…</speed></keep>`. Captions inside the region are timed against the sped-up timeline so they stay in sync. The blender stage also automatically renders a small top-right badge (e.g. `x2.0`) on-screen over every `<speed>` region — disable via `blender.speed_mark.enabled: false`, restyle via `blender.speed_mark.*`, or change the wording via `blender.speed_mark.template` (the `{factor}` placeholder is rendered to one decimal place).

The `<overlay text="...">wrapped transcript words</overlay>` tag places an on-screen TEXT strip in Blender at the wrapped span's time range, with the `text="..."` attribute supplying the on-screen string. Like `<speed>` (and unlike `<keep>`), **it does not force-keep audio** — if the wrapped audio is cut by silence detection, the overlay is silently skipped (there is no timeline content to display it over). The tag may be opened on one line and closed on a later line, so a single overlay can span multiple WhisperX segments; an overlay covering several kept segments (with cut silence between them) displays as one continuous strip across them. Quotes inside the `text="..."` attribute value are not supported. Resume with `./scripts/run_pipeline.sh --from-stage intervals` to apply.

The `<cut>...</cut>` tag deletes the wrapped text. It is a shorthand for `{{wrapped->}}` deletion patches (and can span multiple lines — open on the first, close on the last), so use it to drop whole sentences or sections. There is no separate "cut this time range" mechanism: removing the words opens a gap between the surviving neighbours that the word-gap silence detection cuts from the timeline, so `<cut>` is meant for **larger** deletions (a span shorter than the silence threshold may not actually be cut). Because the text is deleted, no caption is shown for it. Do not overlap `<cut>` with `<keep>`/`<speed>`/`<overlay>` on the same span.

### LLM report (`output/llm_report/`)

Every LLM stage (`text_filter`, `summary`, `plan`, `director`, `guided_edit`)
writes a per-call record under `output/llm_report/`: an `index.md` table
(stage, unit, attempts, outcome, reason) linking to per-call detail files under
`<stage>/<unit>.md` that hold the full prompt and raw response for every attempt,
including retries. Outcomes: `ok`, `ok-empty`, `llm-error`, `unparseable`,
`verify-fail`, `dropped-items` (a call that parsed but discarded some items).
Re-running a stage refreshes only that stage's section. Toggle with
`general.llm_report` (default `true`) and relocate with `general.llm_report_dir`.

### Langfuse tracing (optional)

Every LLM call can be traced to [Langfuse](https://langfuse.com) via LiteLLM's
`langfuse_otel` OTEL callback. Tracing is **off by default** and activates only
when both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set in the
environment.

```bash
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."
./scripts/run_pipeline.sh
```

Set `LANGFUSE_OTEL_HOST` to select a region or self-hosted endpoint (default: US
cloud; EU cloud: `https://cloud.langfuse.com`).

To disable tracing even when keys are present, set `general.langfuse: false` in
your config file, or export `NAGARE_LANGFUSE=0` before running the pipeline.
`run_pipeline.sh` maps the config flag to `NAGARE_LANGFUSE` automatically. Note:
`call_llm` reads only the env var, so `general.langfuse: false` takes effect
**only through `run_pipeline.sh`** — if you invoke a stage CLI directly (e.g.
`python -m nagare_clip.director.cli`) with the keys exported, set
`NAGARE_LANGFUSE=0` yourself to disable.

Traces are grouped by pipeline run (`session_id` = one timestamp per
`run_pipeline.sh` invocation, exported as `NAGARE_RUN_ID`), by stage
(`tags: ["stage:<name>"]`), and by source file (`tags: ["stem:<stem>"]`), so
you can filter by any dimension in the Langfuse UI.

The existing markdown LLM report (`output/llm_report/`) continues to run
alongside Langfuse — the two are independent sinks.

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
# Skip transcription, reuse WhisperX output, re-run silence detection + edits + intervals + Blender
./scripts/run_pipeline.sh --from-stage audio_silence --source myvideo.mp4

# Skip through text_filter, apply silence cuts + text edits and regenerate intervals + Blender project
./scripts/run_pipeline.sh --from-stage intervals --source myvideo.mp4

# Skip through intervals, only regenerate the Blender project
./scripts/run_pipeline.sh --from-stage blender --source myvideo.mp4
```

Override the alignment model (e.g. to revert to the WhisperX built-in default for Japanese):

```bash
./scripts/run_pipeline.sh --align-model jonatasgrosman/wav2vec2-large-xlsr-53-japanese
```

This produces outputs under `output/` (or your `--output-dir`), including:

- `transcription/myvideo.json`, `transcription/myvideo.txt`
- `audio_silence/myvideo_cuts.txt`
- `text_filter/myvideo_edits.txt`
- `intervals/myvideo_intervals.json`
- `blender/myvideo_edited.blend` (named after the first source file)

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

The config file covers all sections, each named after its stage: `general`, `transcription`, `audio_silence`, `text_filter`, `summary`, `plan`, `director`, `guided_edit`, `intervals`, `blender`, `pipeline`. See `config.example.yml` for the full list of keys and their defaults.

### Choosing an LLM provider

Every LLM stage (`text_filter` and its `summary_llm`, `summary`, `plan`, `director`, `guided_edit`) routes through a unified transport backed by the [LiteLLM](https://github.com/BerriAI/litellm) library, so you can point any stage at a local or cloud provider. On a stage's config block, set `provider:` to one of `ollama_chat` (default, local Ollama), `openai`, `gemini`, or `anthropic`, and set `model:` to that provider's model name — LiteLLM receives the combined `"<provider>/<model>"`. Supply credentials with `api_key:` (or the provider's standard environment variable, e.g. `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`). Leave `api_base:` empty for cloud providers; for `ollama_chat` an empty `api_base` falls back to local Ollama (`http://localhost:11434`). Each stage chooses its provider independently, so you can mix (e.g. a cloud model for `director` and local Ollama for `guided_edit`).

```yaml
director:
  enabled: true
  provider: "openai"
  model: "gpt-4o"
  api_key: ""        # or set OPENAI_API_KEY in the environment
  api_base: ""       # leave empty for cloud providers
```

### audio_silence: Audio-Silence Detection

The audio_silence stage runs ffmpeg `silencedetect` (inside the whisperx Docker image) on the waveform and writes an editable `{stem}_cuts.txt` cut list. Each non-comment line is a `START - END` silent span (seconds) that will be cut from the video; delete a line to keep that span, or adjust the times. The intervals stage unions these reviewed ranges into its keep-interval excludes. This is acoustic silence — distinct from `intervals.silence_threshold`, which is a WhisperX word-gap heuristic.

```yaml
audio_silence:
  enabled: true       # false = write an empty cut list (no audio cuts applied)
  noise: -30.0        # ffmpeg silencedetect noise threshold, in dB
  min_silence: 0.8    # minimum silence duration to report, seconds
```

### text_filter: Text Editing (LLM optional)

The text-editing checkpoint always produces `{stem}_edits.txt`. When `text_filter.use_llm` is `false` (default), it copies the transcription `.txt` as-is. When enabled, it runs LLM-based transcription correction and writes output with `{{old->new}}` markers preserved for human review.

```yaml
text_filter:
  use_llm: true
  provider: "ollama_chat"   # see "Choosing an LLM provider" above
  api_base: ""              # empty -> local Ollama; leave empty for cloud providers
  model: "qwen3.5:4b"
  thinking: "low"   # thinking mode: true/false, or "low"/"medium"/"high" for supported models
```

The LLM uses `{{old->new}}` inline patch syntax to mark corrections. Human editors can then review and modify the markers in `_edits.txt` before the intervals stage applies them.

Human editors can also wrap a span in `<keep>...</keep>` to force-preserve the audio under that text. The intervals stage derives the time range from the first wrapped word's start to the last wrapped word's end and carves it out of both the word-gap silence and any overlapping `_cuts.txt` ranges. The marker may be opened on one line and closed on a later line, so a single `<keep>` block can span multiple WhisperX segments and preserve the silences between them. The marker is added after the LLM filter has produced `_edits.txt`, so the LLM never sees it.

`<speed factor="N.N">...</speed>` is a companion marker carrying a playback-speed annotation. Unlike `<keep>`, it does **not** force-keep audio — its span does not carve silence out of the excludes, so silence inside it is still cut and the speed applies only to the surviving spoken parts (nest inside `<keep>` to preserve the audio too). The intervals stage records each marked span as an entry in a top-level `speed_ranges` array (`{start, end, factor}`) in `_intervals.json`, independent of `keep_intervals`. The blender stage splits keep intervals at those boundaries, so a `<speed>` span may cover an arbitrary sub-range of a keep interval (or span several), and adds a Blender VSE Speed Control effect strip over each sped-up sub-range so it plays at the requested speed in the final `.blend`. A `speed_ranges` entry that falls entirely on cut content simply matches no surviving interval and is ignored.

`thinking` enables chain-of-thought reasoning for supported models (e.g. qwen3, deepseek-r1); it maps to LiteLLM's `reasoning_effort` (best-effort per provider). Set `true`/`false`, or a string level like `"low"`, `"medium"`, `"high"` for models that support granular control (e.g. Qwen 3.5). The pipeline uses only the final answer, not the reasoning trace.

#### Summary LLM (optional)

When `text_filter.summary_llm.enabled` is `true`, a separate LLM analyzes the full transcript before filtering to generate a short summary and a list of rare/domain-specific keywords. These are appended to the filter LLM's system prompt so it can better correct mis-dictated words. The summary LLM has its own independent config (model, api_base, temperature, etc.), so you can use a larger model for summarization and a smaller one for filtering.

```yaml
text_filter:
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
- `--language LANG` — ISO 639-1 language code passed to WhisperX (default: `ja`). Also settable via `transcription.language` in config.
- `--from-stage NAME` — start from stage `NAME`, reusing earlier stage outputs. `NAME` is a stage name: `transcription`, `audio_silence`, `text_filter`, `summary`, `plan`, `director`, `guided_edit`, `intervals`, `blender`. Also settable via `pipeline.from_stage` in config.
- `--to-stage NAME` — stop **after** stage `NAME` (inclusive); later stages are skipped. Same stage names as `--from-stage`, and must not precede it. Defaults to `blender` (run to the end). Also settable via `pipeline.to_stage` in config. Combine with `--from-stage` to run a window of stages, e.g. `--from-stage summary --to-stage director`.
- Defaults: input videos under `src_video/`, outputs under `output/`.
- If `--source` contains `/`, it is treated as the exact path; otherwise it is resolved inside `--input-videos-dir`.
- `silence_threshold` and `min_keep` default to `1.5` and `1.0` (overridable via config).
- `pre-margin`/`post-margin` extend keep intervals before/after by default `1.0s` and merge overlaps.
- `--align-model` overrides the HuggingFace model used for WhisperX forced alignment. For Japanese (`ja`), defaults to `vumichien/wav2vec2-large-xlsr-japanese` which showed better alignment scores than the WhisperX built-in default (`jonatasgrosman/wav2vec2-large-xlsr-53-japanese`); for other languages the WhisperX built-in model is used.

## Stage Commands

### transcription only (WhisperX)

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

### audio_silence only (audio-silence detection)

ffmpeg runs inside the whisperx image; capture its stderr, then parse it:

```bash
INPUT_VIDEOS_DIR=src_video OUTPUT_DIR=output \
docker compose run --rm --user "0:0" --entrypoint ffmpeg whisperx \
  -hide_banner -nostats -i "myvideo.mp4" \
  -af "silencedetect=noise=-30.0dB:d=0.8" -f null - \
  >/dev/null 2> output/audio_silence/myvideo_silencedetect.log

uv run python -m nagare_clip.audio_silence.cli \
  --raw output/audio_silence/myvideo_silencedetect.log \
  --output output/audio_silence/myvideo_cuts.txt \
  --config my_project.yml
```

Omit `--raw` (or set `audio_silence.enabled: false`) to write an empty cut list.

### intervals only (patch application + interval generation)

```bash
uv run python -m nagare_clip.intervals.cli \
  --edits-txt output/text_filter/myvideo_edits.txt \
  --json output/transcription/myvideo.json \
  --cuts-txt output/audio_silence/myvideo_cuts.txt \
  --config my_project.yml \
  --output output/intervals/myvideo_intervals.json
```

CLI flags override config file values:

```bash
uv run python -m nagare_clip.intervals.cli \
  --edits-txt output/text_filter/myvideo_edits.txt \
  --json output/transcription/myvideo.json \
  --cuts-txt output/audio_silence/myvideo_cuts.txt \
  --silence_threshold 1.5 \
  --min_keep 1.0 \
  --keep_pre_margin 1.0 \
  --keep_post_margin 1.0 \
  --caption_max_bunsetu 12 \
  --caption_min_bunsetu 3 \
  --caption_max_duration 4.0 \
  --caption_min_duration 1.5 \
  --caption_silence_flush 1.5 \
  --output output/intervals/myvideo_intervals.json

Keep-interval silence detection uses WhisperX word timings (`word.start`/`word.end`) with a per-word max-span cap (0.6s) so inflated token ends do not mask real pauses. Bunsetsu timing uses `ginza.bunsetu_spans(doc)` (GiNZA/spaCy) so particles and auxiliaries are attached to the preceding content word, producing natural subtitle line-break units. It detects large intra-bunsetsu character gaps (> 0.6s) caused by WhisperX misalignment and snaps the bunsetsu start forward to the later character cluster so silence is not hidden inside a single bunsetsu. Caption chunks use bunsetsu-level timing (`end = min(start+0.02s, next_bunsetu_start)`) and are split on detected silence gaps and keep-boundary crossings. Captions are preserved as transcript chunks and the interval stage expands keep intervals to include caption spans so subtitle text is not dropped at the Blender stage, then re-applies minimum keep duration (`--min_keep`) to avoid tiny strips. Tune chunking with `--caption_max_bunsetu`, `--caption_min_bunsetu`, `--caption_max_duration`, `--caption_min_duration`, and `--caption_silence_flush`.
```

### blender only (Blender VSE project)

```bash
blender --background --factory-startup --python-exit-code 1 --python src/nagare_clip/blender/blender_cli.py -- \
  --source src_video/myvideo.mp4 \
  --intervals output/intervals/myvideo_intervals.json \
  --output output/blender/myvideo_edited.blend \
  --config my_project.yml
```

## Operational Notes

- `scripts/run_pipeline.sh` currently runs WhisperX as root (`--user "0:0"`) for compatibility with this image/runtime.
- As a result, transcription output files can be root-owned on host.
- If needed, fix ownership after run:

```bash
sudo chown -R "$(id -u):$(id -g)" output cache
```
