# AGENTS.md

Agent guidance for this repository.

## Objective

Maintain and improve a multi-stage rough-cut pipeline:

1. WhisperX transcription in Docker
2. Audio-silence (jump-cut) detection — ffmpeg `silencedetect`, editable cut list
3. Text editing checkpoint — copies `.txt` or runs LLM filter with `{{old->new}}` markers
4. summary — a larger LLM segments every video into line-range parts + summaries and writes one all-videos summary (project-wide)
5. plan — a larger LLM gives coarse, cross-video rough directions per part (project-wide)
6. director — a larger LLM proposes high-level edits (cut/speed/overlay/keep/edit) as a reviewable JSON op list (fed the summary/plan overview context)
7. guided_edit — a small LLM applies the director's ops into `_edits.txt`, deterministically verified
8. Patch application + keep-interval computation in Python (audio cuts unioned in)
9. Blender VSE auto-layout in headless mode

Final deliverable is a `.blend` project for human editing.

> **Naming convention:** Stages are identified by **functional name**, not
> number (numbers are being phased out — new stages are inserted by name so
> existing stages never need renumbering). Config-section names are the
> identifiers: `transcription:`, `audio_silence:`, `text_filter:`, `summary:`,
> `plan:`, `director:`, `guided_edit:`, `intervals:`, `blender:`. Some Python
> package dirs (`stage2/`, `stage3/`, `stage4/`) keep their legacy numeric names
> to avoid churn; `summary/`, `plan/`, `director/` and `guided_edit/` are
> name-only. `run_pipeline.sh`
> `--from-stage` accepts a stage name (legacy numbers 1-5 still map to
> transcription/audio_silence/text_filter/intervals/blender).

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

`<cut>...</cut>` is a fourth companion marker — a **deletion shorthand**. It
deletes the wrapped text (it desugars to `{{wrapped->}}` deletion patches in
`sync_json._expand_cut_tags`, including whole-line deletion of fully-wrapped
intermediate lines for a multi-line span). There is no separate timeline-exclude
logic: removing the words from the JSON opens a gap between the surviving
neighbours that the interval stage's word-gap silence detection cuts (so a
`<cut>` shorter than `intervals.silence_threshold` may not actually cut — it is
meant for larger deletions). Because the text is deleted, no caption is
generated for it, so caption re-expansion cannot un-cut it. Balance/nesting
rules follow `<keep>`. Do **not** overlap `<cut>` with `<keep>/<speed>/<overlay>`
on the same span.

- **Inputs:** `{stem}.txt`
- **Outputs:** `{stem}_edits.txt`

Humans can validate a hand-edited `_edits.txt` before resuming the intervals stage with the standalone checker `python -m nagare_clip.stage3.check_edits --edits-txt <file> --json <file>` (`src/nagare_clip/stage3/check_edits.py`). Unlike the interval stage's fail-fast `ValueError`, it collects and reports **every** problem at once (line-numbered, exit 1 if any): line-count vs. JSON segments, `{{old->new}}` patch syntax (empty `{{old->}}` deletions allowed; only `{{->}}` flagged), decomposition integrity (reuses `_diagnose_decomposition`, mirroring `sync_json._decompose_edit_line`), and `<keep>`/`<speed>`/`<overlay>`/`<cut>` tag balance / factor>0 / non-empty overlay text / malformed tags. Pure `check_edits(edit_lines, json_data) -> list[Problem]` never raises.

### summary — Project-Wide Summaries

A larger LLM (config `summary:`, disabled by default) runs **once over all videos** (project-wide, not per-source). For each video it segments the clean numbered transcript into contiguous **parts specified by line ranges** and writes a one-sentence summary per part (`summarize.segment_video()`, parsing `{"parts":[{"lines":[a,b],"summary":...}]}`); a reduce step then synthesises one all-videos summary from the per-part summaries (`generate_project_summary()`). `build_summary()` is the map-then-reduce entry point. Reuses `director_llm._coerce_lines`/`_FENCE_RE`/`format_numbered_transcript` and the `llm_retry` bounded-retry helpers; any LLM/parse failure degrades gracefully (empty parts / empty summary). The single `summary.json` (`{summary, parts:[{stem,lines,summary}]}`) is a human-reviewable intermediate consumed by `plan` and `director`. Disabled → writes `{"summary":"","parts":[]}` (no-op).

- **Inputs:** every `{stem}_edits.txt` (from text_filter), via repeated `--edits-txt` (stem derived from basename)
- **Outputs:** `output/summary/summary.json`

### plan — Cross-Video Rough Directions

A larger LLM (config `plan:`, disabled by default) runs **once project-wide** after `summary`. It reads the per-part summaries (with stems + line ranges) plus the overall summary and emits a coarse, **cross-video** editorial direction per part (`plan_llm.generate_plan()`, parsing `{"directions":[{"index":N,"direction":...}]}` and mapping each 1-based index back to its part). Cross-video awareness is the point: a part that repeats an earlier video can be marked "remove". Out-of-range/malformed entries dropped (logged); hard parse failure / LLM error retried via `llm_retry`, then graceful empty. The single `plan.json` (`{directions:[{stem,lines,direction}]}`, self-contained — repeats stem+lines) is human-reviewable and consumed by `director`. Disabled → writes `{"directions":[]}` (no-op).

- **Inputs:** `output/summary/summary.json`
- **Outputs:** `output/plan/plan.json`

### director — LLM High-Level Edit Operations (Pass A)

A larger LLM (config `director:`, disabled by default) reads the whole numbered transcript and emits a JSON op list `{stem}_director.json` — `{"ops": [{type, lines:[a,b], factor?, text?, note}]}`, `type ∈ {cut, speed, overlay, keep, edit}`, lines 1-based. It **never re-outputs the transcript text**, which structurally avoids the "format breakage" and "original modification" failure modes of whole-file editing. `director_llm.parse_director_response()`/`ops_from_dict()` drop any malformed/out-of-range op (logged) so one bad op never derails the rest. The LLM call is retried (config `director.max_retries`, default 2) on a connection error or a **hard parse failure**; `director_llm.try_parse_director_response()` returns `None` only on hard failure (invalid JSON / no `ops` array), so a valid empty `{"ops": []}` is accepted without retry. Each retry nudges temperature up via `nagare_clip.llm_retry.cfg_for_attempt()` (`+retry_temp_step` per attempt, capped at `retry_temp_cap`). After all attempts fail → empty op list (no-op). `_director.json` is a human-reviewable/editable intermediate. When `summary`/`plan` are enabled, the director CLI loads `summary.json`/`plan.json` and (via `director.context.build_director_context(project_summary, directions, stem)`) appends a cross-video **overview context** — the global summary + this video's parts (line ranges + summaries + rough directions) + one-line sibling-video entries — to the director's system prompt (`generate_director_ops(..., overview_context=...)`). When both are disabled/empty the context is `""` and the prompt is byte-identical to before (regression-guarded). `build_director_context` lives in `director/context.py` (not `director_llm.py`) so `summary` can import `director_llm` without a cycle.

- **Inputs:** `{stem}_edits.txt` (from text_filter); optionally `output/summary/summary.json`, `output/plan/plan.json`, `--stem`
- **Outputs:** `{stem}_director.json`

### guided_edit — Apply Director Ops (Pass B2)

A small local LLM (config `guided_edit:`, disabled by default) applies each director op with **one call over just the op's boundary line(s)** (wide ranges show only the first and last line with an omission marker; the open/close tags on the boundaries span the middle automatically). It inserts `<cut>/<speed>/<overlay>/<keep>` tags (and `{{old->new}}` patches for `edit` ops) at the precise position. `reconcile.verify_op()` then checks, per op, that the underlying transcript text was NOT altered (`clean_old()` strips tags + resolves patches to `old`, compared before/after) and that the op was actually reflected; a failing op is retried (config `guided_edit.max_retries`, default 2) on an LLM error or a failed verification, nudging temperature up each attempt via `nagare_clip.llm_retry.cfg_for_attempt()` (`+retry_temp_step`, capped at `retry_temp_cap`). After all attempts fail the op is reverted and recorded in `{stem}_unapplied.txt` with the last failure reason. Disabled → copies `_edits.txt` through unchanged. A final `check_edits` pass (when `--json` is given) logs any residual problems.

- **Inputs:** `{stem}_edits.txt` (text_filter), `{stem}_director.json`, `{stem}.json`
- **Outputs:** augmented `{stem}_edits.txt`, `{stem}_unapplied.txt`

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
- LiteLLM is the LLM transport dependency: all provider access (OpenAI/Gemini/Anthropic/Ollama) goes through `nagare_clip.llm_client.call_llm` — do not add provider-specific HTTP clients.
- Runtime NLP dependency is `ginza` + `ja_ginza` (spaCy-based).
- Route media tooling (ffmpeg) through the existing whisperx Docker image; do not add host binaries or new Python audio deps.
- Preserve the interval JSON (`stage3/` package) as the human-editable contract for the Blender stage.
- The Blender stage must reference original media; do not re-encode/copy source media.

## Project Structure

```
config.example.yml            # Documented YAML config template with all defaults
src/nagare_clip/          # Main Python package (src layout)
  config.py                   # Centralised config loading/merging (DEFAULTS dict)
  llm_retry.py                # Shared bounded-retry helpers (director/guided_edit): retry_attempts(), cfg_for_attempt()
  llm_report.py               # Structured per-call LLM report: Recorder + rebuild_index (index.md + per-call <stage>/<unit>.md)
  llm_client.py               # Unified LiteLLM transport: call_llm(messages, cfg) -> str (OpenAI/Gemini/Anthropic/Ollama)
  cli.py                      # Pipeline Stage 4 CLI entry point (patch + intervals; --cuts-txt)
  __main__.py                 # python -m nagare_clip support
  audio_silence/              # Pipeline Stage 2 (audio-silence detection)
    detect.py                 # parse_silencedetect_output(), build_ffmpeg_args() (pure)
    cuts_file.py              # write_cuts() / read_cuts() — editable cut-list format
    cli.py                    # Pipeline Stage 2 CLI (consumes --raw ffmpeg stderr)
  stage2/                     # text_filter stage modules (text editing checkpoint)
    cli.py                    # text-editing checkpoint CLI entry point
    llm_filter.py             # LLM API calls, {{old->new}} patch parsing, apply_patches_to_lines()
    summary_llm.py            # Summary LLM: generates transcript summary + keywords for filter context
  summary/                    # summary stage (project-wide): per-part + all-videos summaries
    summarize.py              # PartSummary/ProjectSummary, segment_video(), build_summary()
    cli.py                    # summary CLI (repeated --edits-txt -> summary.json)
  plan/                       # plan stage (project-wide): cross-video rough directions
    plan_llm.py               # PartDirection, generate_plan(), plan_to/from_dict()
    cli.py                    # plan CLI (summary.json -> plan.json)
  director/                   # director stage (Pass A): high-level edit ops
    director_llm.py           # DirectorOp, parse/validate JSON ops, generate via LLM
    context.py                # build_director_context(): summary+plan -> prompt overview block
    cli.py                    # director CLI (writes _director.json; --summary/--plan/--stem)
  guided_edit/                # guided_edit stage (Pass B2): apply director ops
    apply.py                  # per-op LLM call + splice + revert-on-failure
    reconcile.py              # verify_op(): verbatim-safety + op-reflection checks
    cli.py                    # guided_edit CLI (writes augmented _edits.txt + _unapplied.txt)
  stage3/                     # intervals stage modules (patch application + intervals)
    check_edits.py            # Standalone _edits.txt integrity checker (reports ALL problems at once)
    sync_json.py              # Sync corrected text back into WhisperX JSON
    bunsetu.py                # Bunsetsu-level timing (GiNZA)
    speech.py                 # Speech span extraction
    intervals.py              # Interval manipulation
    captions.py               # Caption chunking
    filler.py                 # Filler word config (unused at runtime)
    io.py                     # Source file inference
  stage4/                     # blender stage modules (Blender VSE)
    blender_cli.py            # Blender-stage CLI (runs inside Blender)
    scene.py                  # Blender scene setup
    timeline.py               # Strip and caption placement
scripts/
  run_pipeline.sh             # Main orchestrator (name-based stages; --from-stage by name)
tests/
  test_config.py              # Config module unit tests
  test_cli_cuts_merge.py      # --cuts-txt union into interval excludes
  test_cli_keep_markers.py    # <keep>...</keep> force-keep markers (CLI integration)
  test_cli_cut_marker.py      # <cut>...</cut> deletion → silence-gap cut (CLI integration)
  audio_silence/              # audio_silence (detect / cuts_file / cli) unit tests
  stage2/                     # text-editing checkpoint unit tests
  summary/                    # summary segment/build + CLI tests
  plan/                       # plan generate/parse + CLI tests
  director/                   # director op parsing/generation + context + CLI tests
  guided_edit/                # guided_edit apply/reconcile + CLI tests
  stage3/                     # interval-stage unit tests (incl. <cut> desugar)
  stage4/                     # Blender-stage tests
```

## Configuration System

All tunable parameters are centralised in `src/nagare_clip/config.py`:

- `DEFAULTS` dict holds the canonical defaults for all sections.
- `get_effective_config(config_path, cli_overrides)` merges DEFAULTS ← config file ← CLI overrides (highest priority wins).
- `config.example.yml` documents every key with its default value; copy it to start a project config.

**Priority order (highest first):** CLI flags > YAML config file > built-in defaults.

All LLM stages (`text_filter` + its `summary_llm`, `summary`, `plan`, `director`, `guided_edit`) route through `nagare_clip.llm_client.call_llm` (LiteLLM). Each block selects its backend with a `provider` key (default `ollama_chat`); the model id sent to LiteLLM is `"<provider>/<model>"`. `api_base` defaults to `""` (empty): empty + an ollama provider falls back to `http://localhost:11434`, empty + a cloud provider omits `api_base`. `api_key` is forwarded when set (or use the provider's env var). `response_format: "json"` maps to a JSON-object request; `thinking` maps to LiteLLM `reasoning_effort` (best-effort per provider).

`cli.py` (interval stage), `audio_silence/cli.py` (Stage 2), and `blender_cli.py` (Blender stage) all accept a `--config <path>` flag that is passed through by `scripts/run_pipeline.sh` when `--config` is provided.

`scripts/run_pipeline.sh` also reads `pipeline.*`, `transcription.*`, and `audio_silence.*` config keys directly via Python/yaml for arguments that are not forwarded to a Python CLI: `transcription.language` (default `ja`), and `audio_silence.enabled/noise/min_silence` (which decide whether to run the docker ffmpeg call and with what filter params).

## Current Runtime Quirks

- Pipeline Stage 2 (audio-silence) detects acoustic silence via ffmpeg `silencedetect` run in the whisperx container. `run_pipeline.sh` redirects ffmpeg stderr to `{stem}_silencedetect.log`; `audio_silence/detect.py` parses it (duration from ffmpeg's own `Duration:` line — no ffprobe) and `cuts_file.py` writes `{stem}_cuts.txt`. Config: `audio_silence.enabled` (default `true`), `audio_silence.noise` (dB, default `-30.0`), `audio_silence.min_silence` (s, default `0.8`). NOTE: this is acoustic silence, distinct from `intervals.silence_threshold` (a WhisperX word-gap heuristic in the interval stage). `_cuts.txt` is human-editable: blank/`#` lines and malformed/`start>=end` lines are skipped (with a warning) by `read_cuts()`.
- The interval stage (`nagare_clip.cli`) accepts an optional `--cuts-txt`; the parsed ranges are appended to the word-timing `excludes` before clamp/merge/invert, giving a union with the existing detection. Omitting `--cuts-txt` reproduces the prior behaviour exactly (backward compatible; regression-guarded by `tests/test_cli_cuts_merge.py`).
- The `text_filter` config section drives the pipeline-Stage-3 text checkpoint. When `use_llm: false` (default), copies Stage 1 `.txt` to `_edits.txt`. When `use_llm: true`, runs the LLM filter through `nagare_clip.llm_client.call_llm` (LiteLLM); the provider is selected by `text_filter.provider` (default `ollama_chat`, model sent as `<provider>/<model>`), and an empty `api_base` falls back to Ollama localhost (`localhost:11434`) or is omitted for cloud providers. Preserves `{{old->new}}` markers in output. Falls back to original text on any LLM or parse failure. `text_filter.thinking` (default `false`) controls thinking mode: accepts `true`/`false` or a string level (`"low"`, `"medium"`, `"high"`) for models that support it (e.g. Qwen 3.5); it maps to LiteLLM `reasoning_effort` (best-effort per provider).
- Stage 3 optionally runs a **summary LLM** (`text_filter.summary_llm.enabled: true`) before filtering. It sends the full transcript to a (potentially different) LLM that returns a JSON object with `summary` and `keywords` (rare/domain-specific words). These are appended to the filter LLM's system prompt so it can better correct mis-dictated rare words. Routes through `nagare_clip.llm_client.call_llm` (LiteLLM) with `response_format: "json"` mapped to a JSON-object request for reliable parsing. The summary LLM has its own independent config (provider, model, api_base, temperature, etc.). Falls back gracefully if the summary call fails.
- Stage 4 reads `_edits.txt`, applies `{{old->new}}` patches via `apply_patches_to_lines()`, syncs clean text back into WhisperX JSON via `sync_text_to_json()`, then computes intervals.
- Stage 4 also extracts `<keep>...</keep>` ranges from `_edits.txt` via `extract_keep_ranges()` in `src/nagare_clip/stage3/sync_json.py`. The time range is `(first wrapped word.start, last wrapped word.end)` from the post-patch synced JSON; the open/close state is tracked across edit lines so a single `<keep>` block can span multiple segments (the inter-segment silences inside the span are then included in the range). `_first_word_at_or_after` and `_last_word_before` resolve out-of-segment anchor positions by walking to the next/previous segment's word list. Those ranges are subtracted from the unioned excludes (word-gap silence + `_cuts.txt`) via `subtract_intervals()` in `src/nagare_clip/stage3/intervals.py` before merge/invert, so the wrapped audio survives both silence sources. The keep margin config (`intervals.keep_pre_margin` / `keep_post_margin`) does not extend `<keep>` ranges. Empty / unclosed (at EOF) / unmatched / nested / invalid-resolved tags are skipped with a warning. The `<keep>` marker is added by the human after the LLM filter; the LLM never sees it.
- Stage 4 additionally extracts `<speed factor="N.N">...</speed>` ranges via `extract_speed_ranges()` in the same file, returning `(start, end, factor)` triples. These ranges are unioned with `<keep>` ranges before the `subtract_intervals()` call, so a `<speed>` block force-keeps its audio just like `<keep>`. The speed spans are written verbatim to a top-level `speed_ranges` array (`{start, end, factor}`) in the intervals JSON, **independent of `keep_intervals`** (mirroring `overlays`). Stage 5 reads `speed_ranges` and calls `split_intervals_by_speed()` in `src/nagare_clip/stage4/timeline.py`, which cuts each keep interval at every speed-range boundary that falls strictly inside it and annotates each resulting sub-interval with `speed_factor` (the factor of the speed range covering its midpoint, omitted when 1.0/uncovered). This lets a `<speed>` span cover an arbitrary sub-range of a keep interval, or span several. `blender_cli.py` runs the split once per source before `build_timeline_map()`/`place_strips()`, so all downstream functions still operate on per-interval `speed_factor` exactly as before. `place_strips()` applies Blender 5.1 **retiming** (`bpy.ops.sequencer.retiming_segment_speed_set(speed=factor*100)`) directly to both video and audio strips for sped-up sub-intervals, setting `se.active_strip` and `strip.show_retiming_keys = True` before calling the operator; the timeline cursor advances by `round(keep_frame_count / speed)` so subsequent strips/captions line up correctly. No SPEED effect strips are created — retiming shrinks each strip's own duration on ch1/ch2 directly. `build_timeline_map()` and `place_captions()` both divide source-time offsets by `speed_factor` so captions inside a sped-up region display at the corresponding timeline frames. **Blender 5.1 bug:** `strip.retiming_keys` is a shared C pointer across all strips — do not read from it or write to it via Python; always use sequencer operators. **A second facet of the same bug:** even the operator-based `retiming_segment_speed_set` corrupts placed strips' `content_start` once a scene holds several retimed strips at large source-frame offsets — the retimed strip's visible start jumps to `content_start + right-trim` (a shift equal to its un-retimed length) and neighbouring strips are displaced too; durations stay correct, only positions drift. It only surfaced at scale (e.g. the last of 6 sources, ~88 strips with 4 speed spans), not in small scenes. `place_strips()` therefore records each strip's intended timeline start during placement and, in a final **Phase D** pass (after every operator including template deletion has run), re-pins each strip's visible start by setting `content_start = intended_start - left_handle_offset`. The pass is idempotent for uncorrupted strips and is what guarantees captions/overlays (placed at the `build_timeline_map()` positions) align with the actual strips. **This Phase D pass is a workaround for the Blender bug — once a Blender release places retimed strips correctly it becomes a no-op and should be removed together with its regression test.** Regression-guarded by `tests/stage4/test_retiming_positions.py` (+ `blender_retiming_positions.py`), which reproduces the corruption at triggering scale and is verified to fail without Phase D.
- Stage 4 additionally extracts `<overlay text="...">...</overlay>` ranges via
  `extract_overlay_ranges()` in `src/nagare_clip/stage3/sync_json.py`, returning
  `(start, end, text)` triples. Overlays do **not** participate in
  `subtract_intervals()` or speed annotation; they are written verbatim to a
  top-level `overlays` array in the intervals JSON. Stage 5 reads
  `overlays`, maps each through `build_timeline_map()` (speed-aware,
  same trick as captions), and calls `place_overlays()` to create a TEXT strip
  on the overlay channel (`OVERLAY_CHANNEL = 5`, the highest text channel, so
  overlays render on top of captions and speed badges). An overlay spanning
  multiple keep intervals renders as one contiguous TEXT strip across all covered
  intervals (`place_overlays()` accumulates the min `tl_start` / max `tl_end`
  over every matching `tl_map` entry); only an overlay that falls entirely on
  cut content is silently skipped. Style is `caption_style` overlaid with
  `blender.overlay_style` overrides (defaults: `anchor_y: TOP`, `location_y: 0.95`).
- `<cut>...</cut>` is handled entirely in `sync_json._expand_cut_tags()` (called first in `sync_text_to_json`, before the keep/speed/overlay tag-strip): each `<cut>` span is rewritten to `{{wrapped->}}` deletion patches (the wrapped text, with inner patches resolved to their `old` side, becomes the deleted `old`; fully-wrapped intermediate lines become whole-line deletions). The deleted words are then dropped by the existing patch/decompose/timing machinery. There is **no** `extract_cut_ranges()` and **no** intervals-stage change — the cut materialises through word-gap silence detection (`cli.py:250-258`), so it is threshold-dependent and intended for larger deletions. `_patched_visible_length()` strips `<cut>...</cut>` (inner included) so a same-line `<cut>` does not shift neighbouring keep/speed/overlay positions; cross-tag overlap on the same span is unsupported. `check_edits` validates `<cut>` balance via the same `_check_tags` `open_at` mechanism (`"cut"` key). Guarded by `tests/stage3/test_cut_tag.py` and `tests/test_cli_cut_marker.py`.
- Stage 5 reads the existing `speed_ranges` array and, when `blender.speed_mark.enabled` (default true), calls `place_speed_marks()` (`src/nagare_clip/stage4/timeline.py`) once per source after `place_overlays()`. It creates a TEXT badge on the **lowest text channel** (`SPEED_MARK_CHANNEL = 3`, below captions ch4 and overlays ch5) for each speed range, so when the badge overlaps a caption or overlay the badge is the one hidden (it is the least important text). Text = `template.format(factor=f"{factor:.1f}")` (default template `"x{factor}"` → `x2.0`), styled as `caption_style` overlaid with `blender.speed_mark` overrides (default small top-right badge). The three text channels are named constants in `timeline.py` (`SPEED_MARK_CHANNEL = 3` < `CAPTION_CHANNEL = 4` < `OVERLAY_CHANNEL = 5`; video is ch1, audio ch2). Timeline mapping is speed-aware and multi-interval-contiguous, identical to `place_overlays()`. No Stage 1–4 changes — purely a Stage 5 consumer of `speed_ranges`. `resolve_speed_mark_style()` in `blender_cli.py` merges the style and strips the non-style `enabled`/`template` keys.
- Stage 4 keep intervals are expanded by configurable `intervals.keep_pre_margin` / `intervals.keep_post_margin` (defaults 1.0s) and merged before Blender export. Captions have independent `intervals.caption.pre_margin` / `intervals.caption.post_margin` (defaults 0.0s) that extend each caption's display time, clamped against neighbouring caption boundaries so captions never overlap.
- Stage 4 silence-based keep-interval detection uses WhisperX word timings (`word.start`/`word.end`) with a 0.6s per-word span cap so inflated token ends do not hide pauses. The cap is controlled by `intervals.bunsetu.silence_max_word_span` in the config.
- Stage 4 bunsetsu timing (`build_bunsetu_times` in `src/nagare_clip/stage3/bunsetu.py`) uses `ginza.bunsetu_spans(doc)` so particles and auxiliaries attach to the preceding content word, producing natural subtitle line-break units. It detects large intra-bunsetsu character gaps (> 0.6s) caused by WhisperX misalignment and snaps the bunsetsu start forward to the later character cluster so the silence gap is not hidden inside a single bunsetsu. The gap threshold is `intervals.bunsetu.silence_max_word_span`; the end-offset epsilon is `intervals.bunsetu.char_eps`.
- Stage 4 captions are chunked on GiNZA bunsetsu units with bunsetsu-level timing (`end = min(start+char_eps, next start)`), split on silence gaps and keep-boundary crossings; defaults are 12 bunsetsu, 4.0 seconds max, minimum 3 bunsetsu, min duration 1.5s, and silence flush at 1.5s. Bunsetsu units within a chunk are joined with a configurable separator (default `' '`, controlled by `intervals.caption.bunsetu_separator`); a space between units enables word-wrap in Blender TEXT strips.
- Stage 4 captions are preserved as transcript chunks (not pre-filtered by keep intervals), and Stage 4 expands keep intervals to include caption spans so subtitles are retained in Stage 5.
- After caption-based expansion, Stage 4 re-applies `intervals.min_keep` so tiny keep strips are expanded/merged when possible.
- Stage 5 caption style (font size, alignment, position, color, shadow, wrap width, outline, box) is controlled by `blender.caption_style.*` in the config. Options `color` (font fill RGBA, `TextStrip.color`), `use_shadow`, `wrap_width`, `use_outline`, `outline_color`, `outline_width`, `use_box`, and `box_color` have no defaults and are only applied when explicitly set. `color` is applied uniformly across all three text features (`place_captions`/`place_overlays`/`place_speed_marks`), so setting it in `caption_style` colors every text strip; `overlay_style`/`speed_mark` can override it.
- Stage 5 fallback FPS (used when source metadata is unavailable) is controlled by `blender.default_fps`.
- Stage 5 supports multiple source files: `blender_cli.py` accepts repeated `--source`/`--intervals` flags; `place_strips()` and `build_timeline_map()` accept `start_cursor` and `idx_offset` to concatenate sources on a single timeline.
- `run_pipeline.sh` discovers all video files (`mp4`, `mkv`, `mov`, `avi`, `webm`) in `INPUT_VIDEOS_DIR` alphabetically when `--source` is not provided. Multiple `--source` flags are also accepted.
- `run_pipeline.sh` accepts `--from-stage S` where `S` is a stage **name** (`transcription`, `audio_silence`, `text_filter`, `summary`, `plan`, `director`, `guided_edit`, `intervals`, `blender`) to skip expensive earlier stages and reuse their outputs; legacy numbers 1-5 still map to transcription/audio_silence/text_filter/intervals/blender. Stage execution order is the `STAGE_ORDER` array; each stage runs when `FROM_ORDER <= its order`. Also configurable via `pipeline.from_stage` in YAML config. When skipping stages, the script validates that required intermediate outputs exist. Output dirs: legacy `output/stage1`..`output/stage5` for transcription/audio_silence/text_filter/intervals/blender, plus name-only `output/summary/`, `output/plan/`, `output/director/` and `output/guided_edit/`. The intervals stage reads the **guided_edit** `_edits.txt` (which is the text_filter edits passed through when guided_edit is disabled).
- `summary`, `plan`, `director` and `guided_edit` **always run** in a full pipeline (they are cheap no-ops when disabled: summary writes `{summary:"",parts:[]}`, plan writes `{directions:[]}`, director writes an empty op list, guided_edit copies the edits through), so `output/guided_edit/{stem}_edits.txt` always exists for the intervals stage to consume. `summary`/`plan` run **once project-wide** (single invocation over all videos); the other two loop per source.
- Stage 1 (WhisperX) runs in a **single container** for all source files, passing all relative paths as positional arguments. This avoids model reload overhead between videos. Stages 2/3/4 still loop per-source after the single Stage 1 completes.
- All LLM stages (`text_filter` incl. its summary-LLM and batch-halving retries,
  `summary`, `plan`, `director`, `guided_edit`) record every attempt's prompt,
  raw response, retry count and outcome via `nagare_clip.llm_report.Recorder`
  into `output/llm_report/` (config `general.llm_report` default true,
  `general.llm_report_dir` default `output/llm_report`). Each stage CLI builds a
  recorder, `clear()`s its own `<stage>/` subdir at start, passes it into the
  stage functions (default `NULL_RECORDER` = no-op, keeps functions testable),
  and `rebuild_index()`s `index.md` from every detail file's YAML front-matter.
  Outcomes: ok / ok-empty / llm-error / unparseable / verify-fail / dropped-items;
  the parse helpers take a `drops` accumulator so the previously silent per-item
  drop warnings surface as `dropped-items` with counts.

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
- `python -m py_compile` on the Python modules (text_filter `stage2/`, `summary/`, `plan/`, `director/`, `guided_edit/`, intervals `stage3/`, blender `stage4/`)
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
