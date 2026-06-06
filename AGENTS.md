# AGENTS.md

Agent guidance for this repository.

## Objective

Maintain and improve a multi-stage rough-cut pipeline:

1. WhisperX transcription in Docker
2. Audio-silence (jump-cut) detection — ffmpeg `silencedetect`, editable cut list
3. Text editing checkpoint — copies `.txt` or runs LLM filter with `{{old->new}}` markers
4. Patch application + keep-interval computation in Python (audio cuts unioned in)
5. Blender VSE auto-layout in headless mode

Final deliverable is a `.blend` project for human editing.

> **Naming convention:** Python package dirs (`stage2/`, `stage3/`, `stage4/`)
> are kept as-is to avoid churn, but **config-section names are functional**:
> `transcription:` (Stage 1), `audio_silence:` (Stage 2), `text_filter:`
> (Stage 3), `intervals:` (Stage 4), `blender:` (Stage 5).

## Pipeline Overview

`scripts/run_pipeline.sh` orchestrates all stages end-to-end. Use `--from-stage N` to skip earlier stages and reuse their outputs.

### Stage 1 — WhisperX Transcription

Speech-to-text with word-level alignment. Runs in a single Docker container for all source files to avoid model reload overhead.

- **Inputs:** source video files (mp4/mkv/mov/avi/webm)
- **Outputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text)

### Stage 2 — Audio-Silence (Jump-Cut) Detection

Runs ffmpeg `silencedetect` on the waveform **inside the whisperx Docker image** (`docker compose run --entrypoint ffmpeg whisperx`, mirroring Stage 1 — no host/Python ffmpeg dependency). `run_pipeline.sh` captures ffmpeg stderr; `nagare_clip.audio_silence.cli` parses it (`--raw`) and writes an editable `{stem}_cuts.txt`. With `audio_silence.enabled: false` (or no `--raw`) it writes a header-only file (downstream union is a no-op).

- **Inputs:** source video file
- **Outputs:** `{stem}_cuts.txt` (one `START - END` silent span per line; delete a line to keep that span)

### Stage 3 — Text Editing Checkpoint (mandatory)

Produces `{stem}_edits.txt` for human review. When `text_filter.use_llm` is `false` (default), copies the Stage 1 `.txt` as-is. When enabled, runs LLM filter and writes output with `{{old->new}}` markers preserved.

Humans may additionally wrap a span in `<keep>...</keep>` to force-preserve the audio under the wrapped text in Stage 4. The marker may be opened on one line and closed on a later line, so a single `<keep>` block can span multiple WhisperX segments and preserve the inter-segment silences inside it. The tag is added by the human *after* the LLM filter has produced `_edits.txt`; the LLM does not see it.

`<speed factor="N.N">...</speed>` is a companion marker that **additionally** force-preserves the wrapped audio (same mechanism as `<keep>`) AND instructs Stage 5 to play the region at the given playback speed via a Blender VSE Speed Control effect strip. Multi-line spans, nesting/unmatched-tag warnings, and human-after-LLM authorship all follow the `<keep>` rules. `<keep>` is unchanged and remains the shorthand when no speed change is wanted.

`<overlay text="...">...</overlay>` is a third companion marker that places
an on-screen TEXT strip in Stage 5 at the wrapped span's time range. Unlike
`<keep>` and `<speed>`, **it does not force-preserve audio** — if the wrapped
audio is cut by silence detection, the overlay is silently skipped (it cannot
display over content that does not exist on the timeline). Multi-line spans,
nesting/unmatched-tag warnings, and human-after-LLM authorship follow the
`<keep>` rules. Quotes inside the `text="..."` attribute value are not
supported (regex uses `[^"]*`).

- **Inputs:** `{stem}.txt`
- **Outputs:** `{stem}_edits.txt`

### Stage 4 — Patch Application + Keep-Interval Computation

Applies `{{old->new}}` patches from `_edits.txt`, syncs corrected text back into WhisperX JSON timing data, then runs NLP analysis (GiNZA/spaCy bunsetsu segmentation) to compute keep intervals. The Stage 2 `_cuts.txt` ranges are unioned into the exclude set (via `--cuts-txt`) before inversion. Any `<keep>...</keep>` and `<speed factor="...">...</speed>` ranges from `_edits.txt` are then subtracted from the unioned excludes so the wrapped audio survives both silence sources. Speed-marked spans are written verbatim to a top-level `speed_ranges` array in the output JSON (independent of `keep_intervals`); Stage 5 splits keep intervals at those boundaries. All existing caption/min_keep/margin safeguards still apply. Runs per source via `uv run`.

- **Inputs:** `{stem}_edits.txt`, `{stem}.json` (Stage 1 original), `{stem}_cuts.txt` (Stage 2)
- **Outputs:** `{stem}_intervals.json` (keep intervals + captions)

### Stage 5 — Blender VSE Layout

Auto-assembles the rough cut in headless Blender. References original media in-place (no re-encoding). Concatenates all sources onto a single timeline.

- **Inputs:** source video files, `{stem}_intervals.json` for each source
- **Outputs:** `{stem}_edited.blend` — ready for human editing

### Human Editing Workflow

1. Run stages 1–3 → Stage 2 produces `{stem}_cuts.txt`, Stage 3 produces `{stem}_edits.txt`
2. Human edits `_cuts.txt` (delete/adjust silent spans) and `_edits.txt` (`{{old->new}}` patch syntax, optional `<keep>...</keep>` to force-preserve audio, optional `<speed factor="N.N">...</speed>` to force-preserve **and** speed-modify a region in Stage 5, and optional `<overlay text="...">...</overlay>` to place an on-screen TEXT strip in Stage 5 without affecting audio)
3. Resume with `--from-stage 4` → unions cuts, applies patches, syncs JSON, carves out `<keep>`/`<speed>` ranges, computes intervals, runs Blender (with Speed Control effects for `<speed>` regions)

## Hard Constraints

- Dependency management uses uv + pyproject.toml.
- Runtime NLP dependency is `ginza` + `ja_ginza` (spaCy-based).
- Route media tooling (ffmpeg) through the existing whisperx Docker image; do not add host binaries or new Python audio deps.
- Preserve the interval JSON (`stage3/` package) as the human-editable contract for the Blender stage.
- The Blender stage must reference original media; do not re-encode/copy source media.

## Project Structure

```
config.example.yml            # Documented YAML config template with all defaults
src/nagare_clip/          # Main Python package (src layout)
  config.py                   # Centralised config loading/merging (DEFAULTS dict)
  cli.py                      # Pipeline Stage 4 CLI entry point (patch + intervals; --cuts-txt)
  __main__.py                 # python -m nagare_clip support
  audio_silence/              # Pipeline Stage 2 (audio-silence detection)
    detect.py                 # parse_silencedetect_output(), build_ffmpeg_args() (pure)
    cuts_file.py              # write_cuts() / read_cuts() — editable cut-list format
    cli.py                    # Pipeline Stage 2 CLI (consumes --raw ffmpeg stderr)
  stage2/                     # Pipeline Stage 3 modules (text editing checkpoint)
    cli.py                    # text-editing checkpoint CLI entry point
    llm_filter.py             # LLM API calls, {{old->new}} patch parsing, apply_patches_to_lines()
    summary_llm.py            # Summary LLM: generates transcript summary + keywords for filter context
  stage3/                     # Pipeline Stage 4 modules (patch application + intervals)
    sync_json.py              # Sync corrected text back into WhisperX JSON
    bunsetu.py                # Bunsetsu-level timing (GiNZA)
    speech.py                 # Speech span extraction
    intervals.py              # Interval manipulation
    captions.py               # Caption chunking
    filler.py                 # Filler word config (unused at runtime)
    io.py                     # Source file inference
  stage4/                     # Pipeline Stage 5 modules (Blender VSE)
    blender_cli.py            # Blender-stage CLI (runs inside Blender)
    scene.py                  # Blender scene setup
    timeline.py               # Strip and caption placement
scripts/
  run_pipeline.sh             # Main orchestrator (5 stages)
tests/
  test_config.py              # Config module unit tests
  test_cli_cuts_merge.py      # --cuts-txt union into interval excludes
  test_cli_keep_markers.py    # <keep>...</keep> force-keep markers (CLI integration)
  audio_silence/              # Stage 2 (detect / cuts_file / cli) unit tests
  stage2/                     # text-editing checkpoint unit tests
  stage3/                     # interval-stage unit tests
  stage4/                     # Blender-stage tests
```

## Configuration System

All tunable parameters are centralised in `src/nagare_clip/config.py`:

- `DEFAULTS` dict holds the canonical defaults for all sections.
- `get_effective_config(config_path, cli_overrides)` merges DEFAULTS ← config file ← CLI overrides (highest priority wins).
- `config.example.yml` documents every key with its default value; copy it to start a project config.

**Priority order (highest first):** CLI flags > YAML config file > built-in defaults.

`cli.py` (interval stage), `audio_silence/cli.py` (Stage 2), and `blender_cli.py` (Blender stage) all accept a `--config <path>` flag that is passed through by `scripts/run_pipeline.sh` when `--config` is provided.

`scripts/run_pipeline.sh` also reads `pipeline.*`, `transcription.*`, and `audio_silence.*` config keys directly via Python/yaml for arguments that are not forwarded to a Python CLI: `transcription.language` (default `ja`), and `audio_silence.enabled/noise/min_silence` (which decide whether to run the docker ffmpeg call and with what filter params).

## Current Runtime Quirks

- Pipeline Stage 2 (audio-silence) detects acoustic silence via ffmpeg `silencedetect` run in the whisperx container. `run_pipeline.sh` redirects ffmpeg stderr to `{stem}_silencedetect.log`; `audio_silence/detect.py` parses it (duration from ffmpeg's own `Duration:` line — no ffprobe) and `cuts_file.py` writes `{stem}_cuts.txt`. Config: `audio_silence.enabled` (default `true`), `audio_silence.noise` (dB, default `-30.0`), `audio_silence.min_silence` (s, default `0.8`). NOTE: this is acoustic silence, distinct from `intervals.silence_threshold` (a WhisperX word-gap heuristic in the interval stage). `_cuts.txt` is human-editable: blank/`#` lines and malformed/`start>=end` lines are skipped (with a warning) by `read_cuts()`.
- The interval stage (`nagare_clip.cli`) accepts an optional `--cuts-txt`; the parsed ranges are appended to the word-timing `excludes` before clamp/merge/invert, giving a union with the existing detection. Omitting `--cuts-txt` reproduces the prior behaviour exactly (backward compatible; regression-guarded by `tests/test_cli_cuts_merge.py`).
- The `text_filter` config section drives the pipeline-Stage-3 text checkpoint. When `use_llm: false` (default), copies Stage 1 `.txt` to `_edits.txt`. When `use_llm: true`, runs LLM filter via Ollama native chat API (default: `localhost:11434`) and preserves `{{old->new}}` markers in output. Falls back to original text on any LLM or parse failure. `text_filter.thinking` (default `false`) controls thinking mode: accepts `true`/`false` or a string level (`"low"`, `"medium"`, `"high"`) for models that support it (e.g. Qwen 3.5); the value is sent as `"think"` in the API request.
- Stage 3 optionally runs a **summary LLM** (`text_filter.summary_llm.enabled: true`) before filtering. It sends the full transcript to a (potentially different) LLM that returns a JSON object with `summary` and `keywords` (rare/domain-specific words). These are appended to the filter LLM's system prompt so it can better correct mis-dictated rare words. Uses Ollama `format: "json"` for reliable parsing. The summary LLM has its own independent config (model, api_base, temperature, etc.). Falls back gracefully if the summary call fails.
- Stage 4 reads `_edits.txt`, applies `{{old->new}}` patches via `apply_patches_to_lines()`, syncs clean text back into WhisperX JSON via `sync_text_to_json()`, then computes intervals.
- Stage 4 also extracts `<keep>...</keep>` ranges from `_edits.txt` via `extract_keep_ranges()` in `src/nagare_clip/stage3/sync_json.py`. The time range is `(first wrapped word.start, last wrapped word.end)` from the post-patch synced JSON; the open/close state is tracked across edit lines so a single `<keep>` block can span multiple segments (the inter-segment silences inside the span are then included in the range). `_first_word_at_or_after` and `_last_word_before` resolve out-of-segment anchor positions by walking to the next/previous segment's word list. Those ranges are subtracted from the unioned excludes (word-gap silence + `_cuts.txt`) via `subtract_intervals()` in `src/nagare_clip/stage3/intervals.py` before merge/invert, so the wrapped audio survives both silence sources. The keep margin config (`intervals.keep_pre_margin` / `keep_post_margin`) does not extend `<keep>` ranges. Empty / unclosed (at EOF) / unmatched / nested / invalid-resolved tags are skipped with a warning. The `<keep>` marker is added by the human after the LLM filter; the LLM never sees it.
- Stage 4 additionally extracts `<speed factor="N.N">...</speed>` ranges via `extract_speed_ranges()` in the same file, returning `(start, end, factor)` triples. These ranges are unioned with `<keep>` ranges before the `subtract_intervals()` call, so a `<speed>` block force-keeps its audio just like `<keep>`. The speed spans are written verbatim to a top-level `speed_ranges` array (`{start, end, factor}`) in the intervals JSON, **independent of `keep_intervals`** (mirroring `overlays`). Stage 5 reads `speed_ranges` and calls `split_intervals_by_speed()` in `src/nagare_clip/stage4/timeline.py`, which cuts each keep interval at every speed-range boundary that falls strictly inside it and annotates each resulting sub-interval with `speed_factor` (the factor of the speed range covering its midpoint, omitted when 1.0/uncovered). This lets a `<speed>` span cover an arbitrary sub-range of a keep interval, or span several. `blender_cli.py` runs the split once per source before `build_timeline_map()`/`place_strips()`, so all downstream functions still operate on per-interval `speed_factor` exactly as before. `place_strips()` applies a Blender VSE **Speed Control** effect strip (`type="SPEED"`, `input1=movie_strip`, `use_default_fade=False`, `speed_factor=N.N`) per sped-up sub-interval; the timeline cursor advances by `round(keep_frame_count / speed)` so subsequent strips/captions line up correctly. `build_timeline_map()` and `place_captions()` both divide source-time offsets by `speed_factor` so captions inside a sped-up region display at the corresponding timeline frames.
- Stage 4 additionally extracts `<overlay text="...">...</overlay>` ranges via
  `extract_overlay_ranges()` in `src/nagare_clip/stage3/sync_json.py`, returning
  `(start, end, text)` triples. Overlays do **not** participate in
  `subtract_intervals()` or speed annotation; they are written verbatim to a
  top-level `overlays` array in the intervals JSON. Stage 5 reads
  `overlays`, maps each through `build_timeline_map()` (speed-aware,
  same trick as captions), and calls `place_overlays()` to create a TEXT strip
  on channel 4 (one above the caption channel). Overlays that fall on cut
  content are silently skipped. Style is `caption_style` overlaid with
  `blender.overlay_style` overrides (defaults: `anchor_y: TOP`, `location_y: 0.95`).
- Stage 4 keep intervals are expanded by configurable `intervals.keep_pre_margin` / `intervals.keep_post_margin` (defaults 1.0s) and merged before Blender export. Captions have independent `intervals.caption.pre_margin` / `intervals.caption.post_margin` (defaults 0.0s) that extend each caption's display time, clamped against neighbouring caption boundaries so captions never overlap.
- Stage 4 silence-based keep-interval detection uses WhisperX word timings (`word.start`/`word.end`) with a 0.6s per-word span cap so inflated token ends do not hide pauses. The cap is controlled by `intervals.bunsetu.silence_max_word_span` in the config.
- Stage 4 bunsetsu timing (`build_bunsetu_times` in `src/nagare_clip/stage3/bunsetu.py`) uses `ginza.bunsetu_spans(doc)` so particles and auxiliaries attach to the preceding content word, producing natural subtitle line-break units. It detects large intra-bunsetsu character gaps (> 0.6s) caused by WhisperX misalignment and snaps the bunsetsu start forward to the later character cluster so the silence gap is not hidden inside a single bunsetsu. The gap threshold is `intervals.bunsetu.silence_max_word_span`; the end-offset epsilon is `intervals.bunsetu.char_eps`.
- Stage 4 captions are chunked on GiNZA bunsetsu units with bunsetsu-level timing (`end = min(start+char_eps, next start)`), split on silence gaps and keep-boundary crossings; defaults are 12 bunsetsu, 4.0 seconds max, minimum 3 bunsetsu, min duration 1.5s, and silence flush at 1.5s. Bunsetsu units within a chunk are joined with a configurable separator (default `' '`, controlled by `intervals.caption.bunsetu_separator`); a space between units enables word-wrap in Blender TEXT strips.
- Stage 4 captions are preserved as transcript chunks (not pre-filtered by keep intervals), and Stage 4 expands keep intervals to include caption spans so subtitles are retained in Stage 5.
- After caption-based expansion, Stage 4 re-applies `intervals.min_keep` so tiny keep strips are expanded/merged when possible.
- Stage 5 caption style (font size, alignment, position, shadow, wrap width, outline, box) is controlled by `blender.caption_style.*` in the config. Options `use_shadow`, `wrap_width`, `use_outline`, `outline_color`, `outline_width`, `use_box`, and `box_color` have no defaults and are only applied when explicitly set.
- Stage 5 fallback FPS (used when source metadata is unavailable) is controlled by `blender.default_fps`.
- Stage 5 supports multiple source files: `blender_cli.py` accepts repeated `--source`/`--intervals` flags; `place_strips()` and `build_timeline_map()` accept `start_cursor` and `idx_offset` to concatenate sources on a single timeline.
- `run_pipeline.sh` discovers all video files (`mp4`, `mkv`, `mov`, `avi`, `webm`) in `INPUT_VIDEOS_DIR` alphabetically when `--source` is not provided. Multiple `--source` flags are also accepted.
- `run_pipeline.sh` accepts `--from-stage N` (1-5) to skip expensive earlier stages and reuse their outputs. Also configurable via `pipeline.from_stage` in YAML config. When skipping stages, the script validates that required intermediate outputs exist. Output dirs are `output/stage1`..`output/stage5` (cuts → stage2, edits → stage3, intervals → stage4, blend → stage5); resuming pre-renumber projects requires moving files or rerunning from Stage 1.
- Stage 1 (WhisperX) runs in a **single container** for all source files, passing all relative paths as positional arguments. This avoids model reload overhead between videos. Stages 2/3/4 still loop per-source after the single Stage 1 completes.

## Python Execution

Always use `uv run` to invoke Python tools in this repo. Examples:

```bash
uv run pytest
uv run python -m nagare_clip.cli
```

## Preferred Validation

Validation runs automatically via the OpenCode `PostToolUse` hook
defined in `.opencode/plugin/validate.ts`. It triggers after every file
write/edit and runs:

- `docker compose config --services`
- `python -m py_compile` on all Stage 2, Stage 3, and Stage 4 Python modules
- `bash -n scripts/run_pipeline.sh`

If environment allows, also validate with a full run:

```bash
# Single source
./scripts/run_pipeline.sh --source input/<sample>.mp4
# All videos in default directory
./scripts/run_pipeline.sh
```

## Documentation Policy

When behavior changes, update all of:

- `README.md` (user-facing usage)
- `plan.md` (implementation/status)
- this `AGENTS.md` (agent guardrails)
