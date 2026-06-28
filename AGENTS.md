# AGENTS.md

Agent guidance for this repository.

## Objective

Maintain and improve a multi-stage rough-cut pipeline:

1. WhisperX transcription in Docker
2. Audio-silence (jump-cut) detection — ffmpeg `silencedetect`, editable cut list
3. LLM sentence re-segmentation — rewrites `{stem}.json`/`{stem}.txt` into one-sentence-per-line units (disabled by default)
4. Text editing checkpoint — copies `.txt` or runs LLM filter with `{{old->new}}` markers
5. summary — a larger LLM segments every video into line-range parts + summaries and writes one all-videos summary (project-wide)
6. plan — a larger LLM gives coarse, cross-video rough directions per part (project-wide)
7. director — a larger LLM proposes high-level edits (cut/speed/overlay/keep/edit) as a reviewable JSON op list (fed the summary/plan overview context)
8. guided_edit — a small LLM applies the director's ops into `_edits.txt`, deterministically verified
9. Patch application + keep-interval computation in Python (audio cuts unioned in)
10. Blender VSE auto-layout in headless mode

Final deliverable is a `.blend` project for human editing.

> **Naming convention:** Stages are identified only by their **functional /
> config-section name** — there are no stage numbers anywhere. The canonical
> identifiers are: `transcription:`, `audio_silence:`, `sentence_split:`,
> `text_filter:`, `summary:`, `plan:`, `director:`, `guided_edit:`,
> `intervals:`, `blender:`.
> Package dirs (`src/nagare_clip/<name>/`), `output/<name>/` subdirs, and
> `run_pipeline.sh --from-stage`/`--to-stage` all use these same names. A new
> stage can be inserted anywhere without renumbering the others.

## Pipeline Overview

`scripts/run_pipeline.sh` orchestrates all stages end-to-end. Use `--from-stage <name>` to skip earlier stages and reuse their outputs.

### transcription — WhisperX Transcription

Speech-to-text with word-level alignment. Runs in a single Docker container for all source files to avoid model reload overhead.

- **Inputs:** source video files (mp4/mkv/mov/avi/webm)
- **Outputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text)

### audio_silence — Audio-Silence (Jump-Cut) Detection

Runs ffmpeg `silencedetect` on the waveform inside the whisperx Docker image (mirrors the transcription stage — no host/Python ffmpeg dependency). `run_pipeline.sh` captures stderr; `nagare_clip.audio_silence.cli --raw` parses it into an editable `{stem}_cuts.txt`. Disabled (or no `--raw`) → header-only file, so the downstream union is a no-op.

- **Inputs:** source video file
- **Outputs:** `{stem}_cuts.txt` (one `START - END` silent span per line; delete a line to keep that span)

### sentence_split — LLM Sentence Re-Segmentation

An LLM (config `sentence_split:`, disabled by default) rewrites a WhisperX transcript into one-sentence-per-line units per source. Rather than emitting text, the LLM returns **bunsetsu-index ranges** (`{"sentences":[[a,b],…]}`): each range identifies a contiguous span of GiNZA bunsetsu units that form one sentence. `segment.rebuild_window_segments()` slices the original word list at bunsetsu char boundaries (snapped to whole-word boundaries via `char2word`), assembling new segments from the existing words — so word timings are preserved and output text is verbatim by construction. A final concat check (`concat_word_text` before/after) guards against any boundary-rounding discrepancy; if it fails the original segmentation is kept for that window. Processing is windowed (`sentence_split.window_segments`, default 20 segments per LLM call) so long transcripts fit in context; each window degrades independently on LLM failure (returning the original window segments unchanged). Disabled → byte-identical copy-through of `output/transcription/{stem}.{json,txt}`.

- **Inputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text) from transcription
- **Outputs:** re-segmented `{stem}.json` + `{stem}.txt` in `output/sentence_split/`

### text_filter — Text Editing Checkpoint (mandatory)

Produces `{stem}_edits.txt` for human review. When `text_filter.use_llm` is `false` (default), copies the transcription `.txt` as-is. When enabled, runs LLM filter and writes output with `{{old->new}}` markers preserved.

Humans may wrap a span in `<keep>...</keep>` to force-preserve its audio in the intervals stage. It may open on one line and close on a later one, spanning multiple WhisperX segments (and the silences between them). Added by the human *after* the LLM filter runs — the LLM never sees it.

`<speed factor="N.N">...</speed>` plays the region at the given speed in the blender stage. Unlike `<keep>`, it does **not** force-preserve audio — silence inside a `<speed>` span is still cut by silence detection, and the speed applies only to the surviving spoken parts (its span is emitted verbatim in `speed_ranges` regardless, and any portion on cut content is ignored downstream). To keep the audio **and** speed it, nest the tags: `<keep><speed factor="N.N">…</speed></keep>` (the extractors parse each tag type independently and strip the other's tags when counting positions). Multi-line/nesting/authorship rules follow `<keep>`.

`<overlay text="...">...</overlay>` places an on-screen TEXT strip in the blender stage over the wrapped span's time range. Like `<speed>` (and unlike `<keep>`) it does **not** force-preserve audio — if the span is cut by silence detection, the overlay is silently skipped. Multi-line/nesting/authorship rules follow `<keep>`. Quotes inside `text="..."` are unsupported (regex uses `[^"]*`).

`<cut>...</cut>` is a deletion shorthand: it desugars to `{{wrapped->}}` deletion patches (`sync_json._expand_cut_tags`), so the words vanish from the JSON and the resulting gap is cut by the interval stage's word-gap silence detection — meant for deletions longer than `intervals.silence_threshold`, and immune to caption re-expansion since deleted text has no caption. Balance/nesting rules follow `<keep>`; don't overlap it with `<keep>/<speed>/<overlay>` on the same span.

- **Inputs:** `{stem}.txt`
- **Outputs:** `{stem}_edits.txt`

Validate a hand-edited `_edits.txt` before resuming with `python -m nagare_clip.intervals.check_edits --edits-txt <file> --json <file>` (`src/nagare_clip/intervals/check_edits.py`). Unlike the interval stage's fail-fast `ValueError`, it collects **every** problem at once (line-numbered, exit 1 if any): line count vs. JSON segments, `{{old->new}}` syntax, decomposition integrity, and tag balance/validity for all four markers. Pure `check_edits(edit_lines, json_data) -> list[Problem]`, never raises.

### summary — Project-Wide Summaries

A larger LLM (config `summary:`, disabled by default) runs **once project-wide**. For each video it maps the numbered transcript into line-range **parts** with a one-sentence summary each (`summarize.segment_video()`, `{"parts":[{"lines":[a,b],"summary":...}]}`), then reduces all parts into one all-videos summary (`generate_project_summary()`); `build_summary()` is the map-then-reduce entry point. Reuses `director_llm`'s transcript-formatting helpers and `llm_retry`; any failure degrades gracefully to empty parts/summary. `summary.json` (`{summary, parts:[{stem,lines,summary,start?,end?}]}`) is human-reviewable and feeds `plan`/`director`. Each part's optional `start`/`end` (seconds, via `timing.segment_times`) lets `plan` render per-part duration/gap; omitted when timing is unavailable. The CLI takes a repeated `--json` (one WhisperX file per source, matched by stem) to derive those times. Disabled → `{"summary":"","parts":[]}` no-op.

- **Inputs:** every `{stem}_edits.txt` (from text_filter), via repeated `--edits-txt` (stem derived from basename); optionally repeated `--json` (one WhisperX `{stem}.json` per source, for part `start`/`end` times)
- **Outputs:** `output/summary/summary.json`

### plan — Cross-Video Rough Directions

A larger LLM (config `plan:`, disabled by default) runs once project-wide after `summary`, reading all per-part summaries plus the overall summary to emit a coarse **cross-video** direction per part (`plan_llm.generate_plan()`, `{"directions":[{"index":N,"direction":...}]}` mapped back by 1-based index) — e.g. flagging a part that repeats an earlier video as "remove". Context lines render each part's duration + gap-to-next (`N: stem [a-b] [12.4s, gap 1.5s] — summary`, gap shown only within the same video; no JSON is read at this stage, times come from `summary.json`). Out-of-range/malformed entries are dropped (logged); parse/LLM failure retries via `llm_retry`, then degrades to empty. `plan.json` (`{directions:[{stem,lines,direction}]}`, self-contained) feeds `director`. Disabled → `{"directions":[]}` no-op.

- **Inputs:** `output/summary/summary.json`
- **Outputs:** `output/plan/plan.json`

### director — LLM High-Level Edit Operations (Pass A)

A larger LLM (config `director:`, disabled by default) reads the numbered transcript and emits `{stem}_director.json`: `{"ops": [{type, lines:[a,b], factor?, text?, note}]}`, `type ∈ {cut, speed, overlay, keep, edit}`, lines 1-based. It **never re-outputs transcript text**, avoiding whole-file-editing's format-breakage/modification failure modes. Lines are annotated with duration + gap-to-next (e.g. `3: text [4.2s, gap 0.8s]`) from the video's WhisperX JSON via `--json` (`format_numbered_transcript_timed`); falls back to the byte-identical untimed transcript if `--json` is absent or its segment count mismatches. `parse_director_response()`/`ops_from_dict()` drop malformed/out-of-range ops individually (logged). Retried (config `director.max_retries`, default 2) on connection error or hard parse failure only — a valid empty `{"ops": []}` is accepted without retry; each retry nudges temperature up via `llm_retry.cfg_for_attempt()`. All attempts failing → empty op list. When `summary`/`plan` are enabled, `director.context.build_director_context()` appends a cross-video overview (global summary + this video's parts + one-line sibling entries) to the system prompt; disabled/empty → prompt is byte-identical to before (regression-guarded). Lives in `director/context.py`, not `director_llm.py`, so `summary` can import `director_llm` without a cycle.

- **Inputs:** `{stem}_edits.txt` (from text_filter); optionally `{stem}.json` via `--json` (for per-line `[dur, gap]` timing), `output/summary/summary.json`, `output/plan/plan.json`, `--stem`
- **Outputs:** `{stem}_director.json`

### guided_edit — Apply Director Ops (Pass B2)

Applies each director op into `_edits.txt`. **Span ops** (`cut`/`speed`/`overlay`/`keep`) are a pure whole-line-range wrap — the director already fixed the boundaries and the op granularity is whole-line, so `apply.apply_span_op()` deterministically prepends the open tag to the first boundary line and appends the close tag to the last (both on one line for single-line ranges), **with no LLM call**. Existing markers/patches on a line are left intact (nested inside), so the underlying text is unchanged. Because the director reads tag-stripped text (and may overlap its own ops), a span op's range can collide with a **same-type** tag already in the file (human-authored or applied by an earlier op); `apply.clip_range()`/`occupied_lines()` clip the op to the largest contiguous run of lines not already inside a same-type span (ties → earliest) so the tags stay disjoint (same-type nesting is rejected by the intervals extractors). A clip is logged; an op whose whole range is occupied is dropped (logged and recorded in the LLM report). Different-type tags never block each other (each type is parsed independently). Only **`edit` ops** (a within-line `{{old->new}}` the director described only in prose, no explicit old/new) use the small local LLM (config `guided_edit:`, disabled by default), with **one call over just the op's boundary line(s)** (wide ranges show only first/last line with an omission marker). `reconcile.verify_op()` checks both that the underlying text is unaltered (`clean_old()` before/after) and that the op landed — for span ops, the **opening** tag must be on the first boundary line and **closing** on the last (not just present somewhere in range); deterministic application satisfies this by construction, and verification is kept as a guard against a splice bug. A failing `edit` op retries (config `guided_edit.max_retries`, default 2), nudging temperature up via `llm_retry.cfg_for_attempt()`; after all retries fail (or a span op fails verification), the op is reverted and logged (and recorded in the LLM report as `dropped-items`) with the failure reason. Disabled → copies `_edits.txt` through unchanged. A final `check_edits` pass (when `--json` given) logs residual problems.

- **Inputs:** `{stem}_edits.txt` (text_filter), `{stem}_director.json`, `{stem}.json`
- **Outputs:** augmented `{stem}_edits.txt`

### intervals — Patch Application + Keep-Interval Computation

Applies `{{old->new}}` patches from `_edits.txt`, syncs corrected text back into WhisperX JSON timing data, then runs NLP analysis (GiNZA/spaCy bunsetsu segmentation) to compute keep intervals. The audio_silence `_cuts.txt` ranges are unioned into the exclude set (via `--cuts-txt`) before inversion. Any `<keep>...</keep>` ranges from `_edits.txt` are then subtracted from the unioned excludes so the wrapped audio survives both silence sources. `<speed factor="...">...</speed>` spans do **not** force-keep audio — they are written verbatim to a top-level `speed_ranges` array in the output JSON (independent of `keep_intervals`) but are not subtracted from the excludes, so silence inside a bare `<speed>` span is still cut; the blender stage splits keep intervals at those boundaries. All existing caption/min_keep/margin safeguards still apply. Runs per source via `uv run`.

- **Inputs:** `{stem}_edits.txt`, `{stem}.json` (transcription original), `{stem}_cuts.txt` (audio_silence)
- **Outputs:** `{stem}_intervals.json` (keep intervals + captions)

### blender — Blender VSE Layout

Auto-assembles the rough cut in headless Blender. References original media in-place (no re-encoding). Concatenates all sources onto a single timeline.

- **Inputs:** source video files, `{stem}_intervals.json` for each source
- **Outputs:** `{stem}_edited.blend` — ready for human editing

### Human Editing Workflow

1. Run transcription–text_filter → audio_silence produces `{stem}_cuts.txt`, text_filter produces `{stem}_edits.txt`
2. Human edits `_cuts.txt` (delete/adjust silent spans) and `_edits.txt` (`{{old->new}}` patches; optional `<keep>`, `<speed factor="N.N">`, `<overlay text="...">`)
3. Resume with `--from-stage intervals` → unions cuts, applies patches, syncs JSON, carves out `<keep>` ranges (not `<speed>`, which only annotates playback speed), computes intervals, runs Blender (with Speed Control effects for `<speed>` regions)

## Hard Constraints

- Dependency management uses uv + pyproject.toml.
- LiteLLM is the LLM transport dependency: all provider access (OpenAI/Gemini/Anthropic/Ollama) goes through `nagare_clip.llm_client.call_llm` — do not add provider-specific HTTP clients.
- Runtime NLP dependency is `ginza` + `ja_ginza` (spaCy-based).
- Route media tooling (ffmpeg) through the existing whisperx Docker image; do not add host binaries or new Python audio deps.
- Preserve the interval JSON (`intervals/` package) as the human-editable contract for the Blender stage.
- The Blender stage must reference original media; do not re-encode/copy source media.

## Project Structure

```
config.example.yml            # Documented YAML config template with all defaults
src/nagare_clip/          # Main Python package (src layout)
  config.py                   # Centralised config loading/merging (DEFAULTS dict)
  llm_retry.py                # Shared bounded-retry helpers (director/guided_edit): retry_attempts(), cfg_for_attempt()
  llm_report.py               # Structured per-call LLM report: Recorder + rebuild_index (index.md + per-call <stage>/<unit>.md)
  llm_client.py               # Unified LiteLLM transport: call_llm(messages, cfg) -> str (OpenAI/Gemini/Anthropic/Ollama)
  timing.py                   # Pure timing helpers: segment_times(), format_dur_gap() (plan/director duration context)
  __main__.py                 # python -m nagare_clip support (runs the intervals stage)
  audio_silence/              # audio_silence stage (audio-silence detection)
    detect.py                 # parse_silencedetect_output(), build_ffmpeg_args() (pure)
    cuts_file.py              # write_cuts() / read_cuts() — editable cut-list format
    cli.py                    # audio_silence stage CLI (consumes --raw ffmpeg stderr)
  sentence_split/             # sentence_split stage (LLM re-segmentation)
    segment.py                # pure: windowing, char/word map, rebuild_window_segments, verbatim check
    nlp.py                    # GiNZA bunsetsu extraction (lazy import)
    llm.py                    # prompt + bunsetsu-range parse/validate + retry/degrade
    cli.py                    # stage CLI (copy-through when disabled)
  text_filter/                # text_filter stage modules (text editing checkpoint)
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
    cli.py                    # guided_edit CLI (writes augmented _edits.txt)
  intervals/                  # intervals stage modules (patch application + intervals)
    cli.py                    # intervals stage CLI entry point (patch + intervals; --cuts-txt)
    check_edits.py            # Standalone _edits.txt integrity checker (reports ALL problems at once)
    sync_json.py              # Sync corrected text back into WhisperX JSON
    bunsetu.py                # Bunsetsu-level timing (GiNZA)
    speech.py                 # Speech span extraction
    intervals.py              # Interval manipulation
    captions.py                # Caption chunking
    filler.py                 # Filler word config (unused at runtime)
    io.py                     # Source file inference
  blender/                    # blender stage modules (Blender VSE)
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
  sentence_split/             # sentence_split unit + CLI tests
  text_filter/                # text-editing checkpoint unit tests
  summary/                    # summary segment/build + CLI tests
  plan/                       # plan generate/parse + CLI tests
  director/                   # director op parsing/generation + context + CLI tests
  guided_edit/                # guided_edit apply/reconcile + CLI tests
  intervals/                  # interval-stage unit tests (incl. <cut> desugar)
  blender/                    # Blender-stage tests
```

## Configuration System

All tunable parameters are centralised in `src/nagare_clip/config.py`:

- `DEFAULTS` dict holds the canonical defaults for all sections.
- `get_effective_config(config_path, cli_overrides)` merges DEFAULTS ← config file ← CLI overrides (highest priority wins).
- `config.example.yml` documents every key with its default value; copy it to start a project config.

**Priority order (highest first):** CLI flags > YAML config file > built-in defaults.

All LLM stages (`sentence_split`, `text_filter` + its `summary_llm`, `summary`, `plan`, `director`, `guided_edit`) route through `nagare_clip.llm_client.call_llm` (LiteLLM). Each block selects its backend with a `provider` key (default `ollama_chat`); the model id sent to LiteLLM is `"<provider>/<model>"`. An empty `api_base` falls back to `http://localhost:11434` for an ollama provider, or is omitted for a cloud provider. `api_key` is forwarded when set (or use the provider's env var). `response_format: "json"` maps to a JSON-object request; `thinking` maps to LiteLLM `reasoning_effort` (best-effort per provider).

`intervals/cli.py`, `audio_silence/cli.py`, and `blender/blender_cli.py` all accept a `--config <path>` flag, passed through by `scripts/run_pipeline.sh` when `--config` is provided.

`scripts/run_pipeline.sh` also reads `pipeline.*`, `transcription.*`, and `audio_silence.*` config keys directly via Python/yaml for arguments not forwarded to a Python CLI: `transcription.language` (default `ja`), and `audio_silence.enabled/noise/min_silence` (deciding whether/how to run the docker ffmpeg call).

## Current Runtime Quirks

### audio_silence / text_filter

- Audio-silence detection runs ffmpeg `silencedetect` in the whisperx container; `run_pipeline.sh` redirects stderr to `{stem}_silencedetect.log`, `detect.py` parses it (duration from ffmpeg's own `Duration:` line, no ffprobe), `cuts_file.py` writes `{stem}_cuts.txt`. Config: `audio_silence.enabled` (default `true`), `.noise` (dB, default `-30.0`), `.min_silence` (s, default `0.8`) — distinct from `intervals.silence_threshold` (a WhisperX word-gap heuristic in the interval stage). `_cuts.txt` is human-editable; blank/`#`/malformed/`start>=end` lines are skipped with a warning by `read_cuts()`. The interval stage's optional `--cuts-txt` unions these ranges into the word-timing excludes before invert; omitting it reproduces the prior behaviour exactly (regression-guarded by `tests/test_cli_cuts_merge.py`).
- `text_filter.provider` (default `ollama_chat`) selects the LiteLLM backend; an empty `api_base` falls back to Ollama localhost or is omitted for cloud providers. `text_filter.thinking` (default `false`) accepts `true`/`false` or a level string (`"low"/"medium"/"high"`), mapped to LiteLLM `reasoning_effort`. The optional **summary LLM** (`text_filter.summary_llm.enabled`) runs first, sending the full transcript to a (possibly different) LLM that returns `{summary, keywords}`; these are appended to the filter LLM's system prompt to help correct mis-dictated rare words, and fall back gracefully on failure.

### intervals — `<keep>`/`<speed>`/`<overlay>`/`<cut>` markers

- `extract_keep_ranges()` (`intervals/sync_json.py`) resolves `<keep>` to `(first wrapped word.start, last wrapped word.end)` post-patch, tracking open/close across edit lines so one block can span multiple segments (and the silences between them); out-of-segment anchors resolve via `_first_word_at_or_after`/`_last_word_before`. Resolved ranges are subtracted from the unioned excludes via `subtract_intervals()` — `keep_pre_margin`/`keep_post_margin` do **not** extend `<keep>` ranges. Empty/unclosed/unmatched/nested/invalid tags are skipped with a warning.
- `extract_speed_ranges()` (same file) returns `(start, end, factor)` and is written verbatim to a top-level `speed_ranges` array (independent of `keep_intervals`). It is **not** subtracted from the excludes — only `<keep>` ranges are (`cli.py` builds `all_force_keep` from `force_keep_ranges` alone), so silence inside a bare `<speed>` span is still cut. The blender stage's `split_intervals_by_speed()` (`blender/timeline.py`) cuts each surviving keep interval at speed-range boundaries and annotates sub-intervals with `speed_factor` (omitted at 1.0/uncovered), so a `<speed>` span can cover part of an interval or several; a speed range (or the part of one) on cut content matches no interval and is ignored, and `place_speed_marks()` skips a badge whose whole span is cut. Nest `<speed>` inside `<keep>` to preserve the audio too. `place_strips()` applies Blender 5.1 retiming (`retiming_segment_speed_set`) directly to video+audio strips. **`strip.retiming_keys` is a shared C pointer across all strips — never read/write it via Python; always use sequencer operators.** A related Blender bug corrupts placed strips' `content_start` once a scene holds several retimed strips at large offsets (surfaces only at scale, e.g. 6 sources / ~88 strips / 4 speed spans) — `place_strips()` records each strip's intended start and a final **Phase D** pass re-pins `content_start` after every operator has run. **This is a Blender-bug workaround: remove it together with its regression test (`tests/blender/test_retiming_positions.py`) once a Blender release places retimed strips correctly.**
- `extract_overlay_ranges()` returns `(start, end, text)`; overlays skip `subtract_intervals()`/speed annotation entirely and are written verbatim to a top-level `overlays` array. The blender stage's `place_overlays()` renders one contiguous TEXT strip per overlay (even across multiple keep intervals, via min/max `tl_map` bounds) on `OVERLAY_CHANNEL = 5` (topmost text channel); an overlay entirely on cut content is silently skipped. Quotes inside `text="..."` are unsupported.
- `<cut>` is handled entirely in `sync_json._expand_cut_tags()` (runs before the keep/speed/overlay tag-strip): it desugars to `{{wrapped->}}` deletion patches, so there's no `extract_cut_ranges()` or intervals-stage logic — the resulting gap is cut by word-gap silence detection (`cli.py:250-258`), threshold-dependent (deleted text has no caption, so caption re-expansion can't restore it). `_patched_visible_length()` strips `<cut>...</cut>` so it doesn't shift neighbouring tag positions; don't overlap it with other tags on the same span. Guarded by `tests/intervals/test_cut_tag.py` and `tests/test_cli_cut_marker.py`.

### intervals — margins & captions

- `keep_pre_margin`/`keep_post_margin` (default 1.0s) expand keep intervals before merge; `caption.pre_margin`/`caption.post_margin` (default 0.0s) independently extend caption display time, clamped against neighbouring captions.
- Silence detection caps WhisperX word spans at `intervals.bunsetu.silence_max_word_span` (0.6s) so inflated token ends don't hide pauses. `build_bunsetu_times` (`intervals/bunsetu.py`) uses `ginza.bunsetu_spans()` for natural subtitle break units, snapping a bunsetsu's start forward when an intra-bunsetsu character gap exceeds that threshold (WhisperX misalignment); the end-offset epsilon is `intervals.bunsetu.char_eps`.
- Captions chunk on bunsetsu units (defaults: 12 bunsetsu, 4.0s max, 3 bunsetsu min, 1.5s min duration, 1.5s silence flush; joined by `caption.bunsetu_separator`, default `' '`, enabling Blender TEXT-strip word-wrap), are preserved as full transcript chunks (not pre-filtered by keep intervals) with keep intervals expanded to include them, then `intervals.min_keep` is re-applied so tiny keep strips merge/expand.

### blender

- `place_speed_marks()` (when `blender.speed_mark.enabled`, default true) badges each speed range with `template.format(factor=...)` (default `"x{factor}"`) on `SPEED_MARK_CHANNEL = 3` — the lowest of the three text channels (`3` < `CAPTION_CHANNEL=4` < `OVERLAY_CHANNEL=5`), so a badge yields to overlapping captions/overlays.
- Caption/overlay/speed-mark style is `blender.caption_style.*` overlaid with per-feature `overlay_style`/`speed_mark` overrides; `resolve_speed_mark_style()` in `blender_cli.py` merges the style and strips non-style keys. Five layout keys (`font_size`/`alignment_x`/`anchor_y`/`location_x`/`location_y`, in `timeline._MAPPED_STYLE_KEYS`) have per-feature defaults applied explicitly by each placement function. **Every other style key is forwarded verbatim to the Blender TextStrip attribute of the same name** via `timeline.apply_text_style(text_strip, style)` (shared by `place_captions`/`place_overlays`/`place_speed_marks`) — so any `bpy.types.TextStrip` attribute (`shadow_color`/`shadow_offset`/`shadow_blur`/`shadow_angle`, `box_margin`/`box_roundness`, the older `color`/`use_shadow`/`use_outline`/`outline_*`/`use_box`/`box_color`/`wrap_width`, ...) is configurable with no code change. A key that isn't a valid strip attribute raises `AttributeError` on assignment (Blender RNA rejects unknown properties) and is logged-and-skipped. The one exception is **`font`**: the TextStrip `font` attribute is a `VectorFont` datablock (not a path string), so `apply_text_style` special-cases it via `timeline._load_font(path)` — the value must be an **absolute** filepath that exists; it's loaded with `bpy.data.fonts.load(path, check_existing=True)` and assigned. An invalid (non-absolute/non-str) or missing path is a **hard error** (`ValueError`/`FileNotFoundError`), failing the blender stage rather than silently falling back to the default font. Guarded by `tests/blender/test_text_style.py`.
- `blender.default_fps` is the fallback FPS when source metadata is unavailable. Multiple sources concatenate onto one timeline via repeated `--source`/`--intervals` and `start_cursor`/`idx_offset`.

### Pipeline orchestration

- `run_pipeline.sh` discovers videos (`mp4/mkv/mov/avi/webm`) alphabetically in `INPUT_VIDEOS_DIR` when `--source` is omitted (repeatable). `--from-stage`/`--to-stage` take a stage name; the window is enforced by `STAGE_ORDER`/`in_window`/`past_window`, skipping earlier stages validates required outputs exist, and `--from-stage` after `--to-stage` errors. (`stage_index` deliberately returns success even for an unknown name, so under `set -e` an invalid stage reaches its own friendly error message instead of aborting silently.) Also configurable via `pipeline.from_stage`/`to_stage`. Output dirs are one per stage by name: `output/transcription|audio_silence|text_filter|summary|plan|director|guided_edit|intervals|blender/`.
- `sentence_split` **always runs** per source (copy-through when disabled), so `output/sentence_split/{stem}.json` and `output/sentence_split/{stem}.txt` always exist. All downstream `.json`/`.txt` consumers (`text_filter`, `summary`, `director`, `guided_edit`, `intervals`) read from `output/sentence_split/` rather than `output/transcription/`.
- `summary`/`plan`/`director`/`guided_edit` **always run** (cheap no-ops when disabled), so `output/guided_edit/{stem}_edits.txt` always exists for the intervals stage; `summary`/`plan` run once project-wide, the other two loop per source. WhisperX (transcription) also runs in a single container for all sources to avoid model-reload overhead; the remaining per-source stages still loop afterwards.
- `timing.py` is a pure helper (`segment_times()`, `format_dur_gap()`) used by `summary` (per-part `start`/`end`) and `director` (per-line `[dur, gap]` annotations); `plan` reads the precomputed times from `summary.json` rather than touching JSON itself. `run_pipeline.sh` passes `--json` to both the summary and director stages.
- All LLM stages record every attempt (prompt, raw response, retries, outcome, plus call config like `model`/`thinking`) via `llm_report.Recorder` into `output/llm_report/` (config `general.llm_report`/`llm_report_dir`); each stage CLI clears its own subdir, passes the recorder through (default `NULL_RECORDER` keeps functions testable), and rebuilds `index.md` from front-matter. Outcomes: ok / ok-empty / llm-error / unparseable / verify-fail / dropped-items (the last surfaces previously-silent per-item drop warnings with counts).

### llm_client — Langfuse tracing

- `call_llm` (the single LLM chokepoint) registers LiteLLM's `langfuse_otel` callback exactly once per process via `_ensure_tracing()` and attaches per-call grouping metadata when tracing is enabled: `generation_name="<stage>/<unit>"`, `tags=["stage:<stage>","stem:<unit>"]`, `session_id=NAGARE_RUN_ID`. Metadata is carried via `with_trace_meta(cfg, stage=..., unit=...)` under a reserved `cfg["_trace"]` key that `call_llm` pops before passing kwargs to the provider — so the provider call is byte-identical when tracing is disabled. Tracing is enabled iff `LANGFUSE_PUBLIC_KEY` **and** `LANGFUSE_SECRET_KEY` are set in the environment **and** `NAGARE_LANGFUSE != "0"` **and** `general.langfuse` (config, default `true`) is not false; `run_pipeline.sh` exports `NAGARE_RUN_ID` (one timestamp per run) and maps `general.langfuse: false` to `export NAGARE_LANGFUSE=0` for all subprocesses. `LANGFUSE_OTEL_HOST` selects region / self-hosted endpoint (default: US cloud; EU: `https://cloud.langfuse.com`). Short-lived stage CLIs flush pending spans via an `atexit` hook (`flush_traces()` calls `provider.force_flush()` best-effort, never raises); if OTEL flushing proves unreliable the documented fallback is to switch the callback to the langfuse-SDK integration (`["langfuse"]`) and call `langfuse.flush()` in `flush_traces`. The markdown `llm_report` runs alongside as an independent sink (untouched). This is observability-only and decoupled from any future LangGraph migration.

## Python Execution

Always use `uv run` to invoke Python tools in this repo. Examples:

```bash
uv run pytest
uv run python -m nagare_clip.intervals.cli
```

## Preferred Validation

Validation runs automatically via the OpenCode `PostToolUse` hook
defined in `.opencode/plugin/validate.ts`. It triggers after every file
write/edit and runs:

- `docker compose config --services`
- `python -m py_compile` on the Python modules (every file under `src/nagare_clip/`)
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
