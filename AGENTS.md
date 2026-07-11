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

`scripts/run_pipeline.sh` is a thin shim that execs `uv run python -m nagare_clip.pipeline "$@"` — the orchestration itself lives in the Python package `src/nagare_clip/pipeline/` (see [`docs/stages/pipeline.md`](docs/stages/pipeline.md)). Use `--from-stage <name>` to skip earlier stages and reuse their outputs.

### transcription — WhisperX Transcription

Speech-to-text with word-level alignment. Runs in a single Docker container for all source files to avoid model reload overhead.

- **Inputs:** source video files (mp4/mkv/mov/avi/webm)
- **Outputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text)

### audio_silence — Audio-Silence (Jump-Cut) Detection

Runs ffmpeg `silencedetect` on the waveform inside the whisperx Docker image (mirrors the transcription stage — no host/Python ffmpeg dependency). The pipeline orchestrator captures stderr to a log file and passes it to `audio_silence.run.run_audio_silence()` as `raw_path`, which parses it into an editable `{stem}_cuts.txt`. Disabled (or no captured output) → header-only file, so the downstream union is a no-op.

- **Inputs:** source video file
- **Outputs:** `{stem}_cuts.txt` (one `START - END` silent span per line; delete a line to keep that span)

### sentence_split — LLM Sentence Re-Segmentation

An LLM (config `sentence_split:`, disabled by default) rewrites a WhisperX transcript into one-sentence-per-line units per source. Rather than emitting text, the LLM returns **bunsetsu-index ranges** (`{"sentences":[[a,b],…]}`): each range identifies a contiguous span of GiNZA bunsetsu units that form one sentence. `segment.rebuild_window_segments()` slices the original word list at bunsetsu char boundaries (snapped to whole-word boundaries via `char2word`), assembling new segments from the existing words — so word timings are preserved and output text is verbatim by construction. A final concat check (`concat_word_text` before/after) guards against any boundary-rounding discrepancy; if it fails the original segmentation is kept for that window. Processing is windowed (`sentence_split.window_segments`, default 20 segments per LLM call — this is the batch size) so long transcripts fit in context; each window degrades independently on LLM failure (returning the original window segments unchanged). Windows are non-overlapping but **carry the trailing sentence over the seam**: all sentences of a window except the last are emitted, and the last (possibly incomplete) sentence's words are prepended to the next window so a sentence straddling a window boundary is re-grouped with its continuation instead of being forced to split (`resegment_json`). A single-sentence window is the exception — it emits as-is and resets the carry (run-on guard, bounding context growth), so a sentence longer than a full window still splits at the seam. On degrade, any carried-in sentence is flushed as a finalized segment before falling back to the window's original segments, so carry-over never loses words (the global `concat_word_text` invariant still guards the whole result). Disabled → byte-identical copy-through of `output/transcription/{stem}.{json,txt}`.

**Force split at long silences** (config `force_split`, default true): the audio_silence `{stem}_cuts.txt` is read (`read_cuts`) and spans ≥ `force_split_min_silence` seconds (default 3.0 — deliberately higher than `audio_silence.min_silence`, which targets short jump-cut pauses) become hard sentence boundaries. `resegment_json` post-splits **every** emitted segment — LLM-rebuilt, carry-flush, and degrade-fallback — via `segment.split_segment_at_silences()`, which cuts before the first word whose (start-time, inheriting the last known time for untimed words) is ≥ the silence midpoint, **only when the previous word's end-time is ≤ that midpoint** — i.e. a genuine inter-word gap corroborates the silence. WhisperX often stretches one word across an entire pause (its end abutting the next word); the midpoint then falls *inside* that word, which sentence it belongs to is undecidable from timing, and splitting anyway would chop the sentence's first characters off — so such silences are skipped (false negative over wrong split). Pure post-enforcement: window contents and the LLM call count are byte-identical to before (`silences=[]` is a regression-guarded no-op), the split survives total LLM failure, and because it runs before the carry decision, carry-over can never drag words across a silence. Threshold filtering happens once in `run_sentence_split` (`_forced_silences`); the pure `split_segment_at_silences` receives only already-qualifying spans. Since the cuts file is human-editable, deleting a line (keeping that span's audio) also stops it forcing a split.

- **Inputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text) from transcription; the audio_silence `{stem}_cuts.txt`, passed as `cuts_txt` (for force-split silences)
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

A larger LLM (config `summary:`, disabled by default) runs **once project-wide**. For each video it maps the numbered transcript into line-range **parts** with a one-sentence summary each (`summarize.segment_video()`, `{"parts":[{"lines":[a,b],"summary":...}]}`), then reduces all parts into one all-videos summary (`generate_project_summary()`); `build_summary()` is the map-then-reduce entry point. Reuses `director_llm`'s transcript-formatting helpers and `llm_retry`; any failure degrades gracefully to empty parts/summary. `summary.json` (`{summary, parts:[{stem,lines,summary,start?,end?}]}`) is human-reviewable and feeds `plan`/`director`. Each part's optional `start`/`end` (seconds, via `timing.segment_times`) lets `plan` render per-part duration/gap; omitted when timing is unavailable. The orchestrator passes each source's sentence_split `{stem}.json` via `run_summary(..., json_paths=...)` to derive those times. Disabled → `{"summary":"","parts":[]}` no-op.

- **Inputs:** every `{stem}_edits.txt` (from text_filter), passed as `edits_txts` (stem derived from basename); optionally the sentence_split `{stem}.json` per source, passed as `json_paths` (for part `start`/`end` times)
- **Outputs:** `output/summary/summary.json`

### plan — Cross-Video Rough Directions

A larger LLM (config `plan:`, disabled by default) runs once project-wide after `summary`, reading all per-part summaries plus the overall summary to emit a coarse **cross-video** direction per part (`plan_llm.generate_plan()`, `{"directions":[{"index":N,"direction":...}]}` mapped back by 1-based index) — e.g. flagging a part that repeats an earlier video as "remove". Context lines render each part's duration + gap-to-next (`N: stem [a-b] [12.4s, gap 1.5s] — summary`, gap shown only within the same video; no JSON is read at this stage, times come from `summary.json`). Out-of-range/malformed entries are dropped (logged); parse/LLM failure retries via `llm_retry`, then degrades to empty. `plan.json` (`{directions:[{stem,lines,direction}]}`, self-contained) feeds `director`. Disabled → `{"directions":[]}` no-op.

- **Inputs:** `output/summary/summary.json`
- **Outputs:** `output/plan/plan.json`

### director — LLM High-Level Edit Operations (Pass A)

A larger LLM (config `director:`, disabled by default) reads the numbered transcript and emits `{stem}_director.json`: `{"ops": [{type, lines:[a,b], factor?, text?, note}]}`, `type ∈ {cut, speed, overlay, keep, edit}`, lines 1-based. It **never re-outputs transcript text**, avoiding whole-file-editing's format-breakage/modification failure modes. Lines are annotated with duration + gap-to-next (e.g. `3: text [4.2s, gap 0.8s]`) from the video's WhisperX JSON, passed by the orchestrator as `json_path` (`format_numbered_transcript_timed`); falls back to the byte-identical untimed transcript if `json_path` is absent or its segment count mismatches. `parse_director_response()`/`ops_from_dict()` drop malformed/out-of-range ops individually (logged). Retried (config `director.max_retries`, default 2) on connection error or hard parse failure only — a valid empty `{"ops": []}` is accepted without retry; each retry nudges temperature up via `llm_retry.cfg_for_attempt()`. All attempts failing → empty op list. When `summary`/`plan` are enabled, `director.context.build_director_context()` appends a cross-video overview (global summary + this video's parts + one-line sibling entries) to the system prompt; disabled/empty → prompt is byte-identical to before (regression-guarded). Lives in `director/context.py`, not `director_llm.py`, so `summary` can import `director_llm` without a cycle.

- **Inputs:** `{stem}_edits.txt` (from text_filter); optionally the sentence_split `{stem}.json`, passed as `json_path` (for per-line `[dur, gap]` timing), `output/summary/summary.json`, `output/plan/plan.json`, and the source stem, passed as `stem`
- **Outputs:** `{stem}_director.json`

### guided_edit — Apply Director Ops (Pass B2)

Applies each director op into `_edits.txt`. **Span ops** (`cut`/`speed`/`overlay`/`keep`) are a pure whole-line-range wrap — the director already fixed the boundaries and the op granularity is whole-line, so `apply.apply_span_op()` deterministically prepends the open tag to the first boundary line and appends the close tag to the last (both on one line for single-line ranges), **with no LLM call**. Existing markers/patches on a line are left intact (nested inside), so the underlying text is unchanged. Because the director reads tag-stripped text (and may overlap its own ops), a span op's range can collide with a **same-type** tag already in the file (human-authored or applied by an earlier op); `apply.clip_range()`/`occupied_lines()` clip the op to the largest contiguous run of lines not already inside a same-type span (ties → earliest) so the tags stay disjoint (same-type nesting is rejected by the intervals extractors). A clip is logged; an op whose whole range is occupied is dropped (logged and recorded in the LLM report). Different-type tags never block each other (each type is parsed independently). Only **`edit` ops** (a within-line `{{old->new}}` the director described only in prose, no explicit old/new) use the small local LLM (config `guided_edit:`, disabled by default), with **one call over just the op's boundary line(s)** (wide ranges show only first/last line with an omission marker). `reconcile.verify_op()` checks both that the underlying text is unaltered (`clean_old()` before/after) and that the op landed — for span ops, the **opening** tag must be on the first boundary line and **closing** on the last (not just present somewhere in range); deterministic application satisfies this by construction, and verification is kept as a guard against a splice bug. A failing `edit` op retries (config `guided_edit.max_retries`, default 2), nudging temperature up via `llm_retry.cfg_for_attempt()`; after all retries fail (or a span op fails verification), the op is reverted and logged (and recorded in the LLM report as `dropped-items`) with the failure reason. Disabled → copies `_edits.txt` through unchanged. A final `check_edits` pass (when `json_path` is given — the orchestrator always passes it) logs residual problems.

- **Inputs:** `{stem}_edits.txt` (text_filter), `{stem}_director.json`, `{stem}.json`
- **Outputs:** augmented `{stem}_edits.txt`

### intervals — Patch Application + Keep-Interval Computation

Applies `{{old->new}}` patches from `_edits.txt`, syncs corrected text back into WhisperX JSON timing data, then runs NLP analysis (GiNZA/spaCy bunsetsu segmentation) to compute keep intervals. The audio_silence `_cuts.txt` ranges are unioned into the exclude set (via the `cuts_txt` argument) before inversion. Any `<keep>...</keep>` ranges from `_edits.txt` are then subtracted from the unioned excludes so the wrapped audio survives both silence sources. `<speed factor="...">...</speed>` spans do **not** force-keep audio — they are written verbatim to a top-level `speed_ranges` array in the output JSON (independent of `keep_intervals`) but are not subtracted from the excludes, so silence inside a bare `<speed>` span is still cut; the blender stage splits keep intervals at those boundaries. All existing caption/min_keep/margin safeguards still apply. Runs per source in-process within the pipeline.

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
  __main__.py                 # python -m nagare_clip support (re-aliased to the pipeline CLI)
  pipeline/                   # pipeline orchestrator (replaces bash run_pipeline.sh)
    errors.py                 # PipelineError — user-facing orchestration failure
    sources.py                # SourceMedia; discover/resolve/stage source videos for Docker
    external.py               # docker/blender command builders + run_command() — the only subprocesses
    runner.py                 # Stage dataclass, PipelineContext, resolve_window(), run_stages() (windowing/skip-validation)
    stages.py                 # STAGE_NAMES + one adapter per stage + STAGES registry (owns per-stage LLM-report recorder lifecycle)
    cli.py                    # pipeline CLI: same flags as the old bash script; CLI > YAML > defaults via get_effective_config
    __main__.py                # python -m nagare_clip.pipeline entry point
  audio_silence/              # audio_silence stage (audio-silence detection)
    detect.py                 # parse_silencedetect_output(), build_ffmpeg_args() (pure)
    cuts_file.py              # write_cuts() / read_cuts() — editable cut-list format
    run.py                    # run_audio_silence() — consumes captured ffmpeg stderr
  sentence_split/             # sentence_split stage (LLM re-segmentation)
    segment.py                # pure: windowing, char/word map, rebuild_window_segments, verbatim check
    nlp.py                    # GiNZA bunsetsu extraction (lazy import)
    llm.py                    # prompt + bunsetsu-range parse/validate + retry/degrade
    run.py                    # run_sentence_split() (copy-through when disabled)
  text_filter/                # text_filter stage modules (text editing checkpoint)
    run.py                    # run_text_filter() typed entry point
    llm_filter.py             # LLM API calls, {{old->new}} patch parsing, apply_patches_to_lines()
    summary_llm.py            # Summary LLM: generates transcript summary + keywords for filter context
  summary/                    # summary stage (project-wide): per-part + all-videos summaries
    summarize.py              # PartSummary/ProjectSummary, segment_video(), build_summary()
    run.py                    # run_summary() (repeated edits-txt/json paths -> summary.json)
  plan/                       # plan stage (project-wide): cross-video rough directions
    plan_llm.py               # PartDirection, generate_plan(), plan_to/from_dict()
    run.py                    # run_plan() (summary.json -> plan.json)
  director/                   # director stage (Pass A): high-level edit ops
    director_llm.py           # DirectorOp, parse/validate JSON ops, generate via LLM
    context.py                # build_director_context(): summary+plan -> prompt overview block
    run.py                    # run_director() (writes _director.json; summary/plan/stem/json_path)
  guided_edit/                # guided_edit stage (Pass B2): apply director ops
    apply.py                  # per-op LLM call + splice + revert-on-failure
    reconcile.py              # verify_op(): verbatim-safety + op-reflection checks
    run.py                    # run_guided_edit() (writes augmented _edits.txt)
  intervals/                  # intervals stage modules (patch application + intervals)
    run.py                    # run_intervals() typed entry point (patch + intervals; cuts_txt)
    check_edits.py            # Standalone _edits.txt integrity checker (reports ALL problems at once)
    sync_json.py              # Sync corrected text back into WhisperX JSON
    bunsetu.py                # Bunsetsu-level timing (GiNZA)
    speech.py                 # Speech span extraction
    intervals.py              # Interval manipulation
    captions.py                # Caption chunking
    io.py                     # Source file inference
  blender/                    # blender stage modules (Blender VSE)
    blender_cli.py            # Blender-stage CLI (separate process, runs inside Blender)
    scene.py                  # Blender scene setup
    timeline.py               # Strip and caption placement
scripts/
  run_pipeline.sh             # Shim: exec uv run python -m nagare_clip.pipeline "$@"
docs/
  stages/                     # Deep per-stage runtime notes (loaded on demand)
Makefile                      # Canonical dev commands (make help / check / validate / test)
.github/workflows/ci.yml      # CI: ruff lint + format check, shell syntax, pytest
tests/
  test_config.py              # Config module unit tests + example-config sync lint
  pipeline/                   # pipeline orchestrator tests: cli/external/runner/sources/stages
  audio_silence/              # audio_silence (detect / cuts_file / run) unit tests
  sentence_split/             # sentence_split unit + run() tests
  text_filter/                # text-editing checkpoint unit tests
  summary/                    # summary segment/build + run() tests
  plan/                       # plan generate/parse + run() tests
  director/                   # director op parsing/generation + context + run() tests
  guided_edit/                # guided_edit apply/reconcile + run() tests
  intervals/                  # interval-stage unit tests (incl. <keep>/<cut> markers, cuts_txt union)
  blender/                    # Blender-stage tests
```

## Configuration System

All tunable parameters are defined as typed **pydantic-settings models** in
`src/nagare_clip/config.py` (one `BaseModel` per section, composed by
`NagareClipConfig`):

- The models are the single source of truth for defaults, types, and docs.
  `DEFAULTS` is **derived** (`NagareClipConfig.model_validate({}).model_dump()`).
- `get_effective_config(config_path, cli_overrides)` merges defaults ← file ←
  CLI, then **validates**: unknown or wrongly-typed keys raise `ValidationError`
  (so a typo no longer vanishes silently). Exception: `blender.caption_style`,
  `overlay_style`, and `speed_mark` use `extra="allow"` — they are open-ended
  Blender TextStrip pass-throughs (any RNA attribute incl. `font`).
- It still returns a plain `dict` (the "dict boundary"), so all stage `run()`
  functions and `call_llm`/`llm_retry` are unchanged.
- `config.example.yml` is **generated** from the models — run
  `make config-example`. A test (`tests/test_config.py::test_example_file_matches_generator`)
  fails if the committed file drifts from the generator output.

**Priority order (highest first):** CLI flags > YAML config file > model defaults.

All LLM stages (`sentence_split`, `text_filter` + its `summary_llm`, `summary`, `plan`, `director`, `guided_edit`) route through `nagare_clip.llm_client.call_llm` (LiteLLM). Each block selects its backend with a `provider` key (default `ollama_chat`); the model id sent to LiteLLM is `"<provider>/<model>"`. An empty `api_base` falls back to `http://localhost:11434` for an ollama provider, or is omitted for a cloud provider. `api_key` is forwarded when set (or use the provider's env var). `response_format: "json"` maps to a JSON-object request; `thinking` maps to LiteLLM `reasoning_effort` (best-effort per provider).

Only `blender/blender_cli.py` still takes a `--config <path>` flag on its command line — it runs as a separate Blender subprocess, so the pipeline CLI (`nagare_clip.pipeline.cli`) passes its resolved `config_path` through explicitly. Every other stage receives the already-merged `cfg` dict in-process (no subprocess, no re-parsing of `--config`).

## Current Runtime Quirks

Deep per-stage implementation detail — edge cases, invariants, and Blender-bug
workarounds — lives in [`docs/stages/`](docs/stages/), loaded on demand. **Read
the relevant file before touching a stage:**

- audio_silence → [`docs/stages/audio_silence.md`](docs/stages/audio_silence.md)
- text_filter (+ summary LLM) → [`docs/stages/text_filter.md`](docs/stages/text_filter.md)
- intervals (`<keep>`/`<speed>`/`<overlay>`/`<cut>` markers, margins, captions) → [`docs/stages/intervals.md`](docs/stages/intervals.md)
- blender (VSE layout, text styling, retiming) → [`docs/stages/blender.md`](docs/stages/blender.md)
- pipeline orchestration (`nagare_clip.pipeline`) → [`docs/stages/pipeline.md`](docs/stages/pipeline.md)
- observability (LLM report + Langfuse tracing) → [`docs/stages/observability.md`](docs/stages/observability.md)

## Python Execution

Always use `uv run` to invoke Python tools in this repo. Examples:

```bash
uv run pytest
uv run python -m nagare_clip.pipeline --from-stage intervals --to-stage intervals
```

## Preferred Validation

Canonical developer commands live in the `Makefile` (run `make help`):

- `make check` — everything CI runs: `lint` + `format-check` + `validate` + `test`.
- `make validate` — fast structural checks: `docker compose config`, `python -m py_compile` on every module under `src/nagare_clip/`, and `bash -n scripts/run_pipeline.sh`.
- `make lint` / `make format` — ruff lint / auto-format.
- `make test` — the pytest suite.

The same steps run in CI (`.github/workflows/ci.yml`) on every push and PR.

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
