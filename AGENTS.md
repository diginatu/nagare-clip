# AGENTS.md

Agent guidance for this repository.

## Objective

Maintain and improve a multi-stage rough-cut pipeline:

1. WhisperX transcription in Docker
2. Text editing checkpoint — copies `.txt` or runs LLM filter with `{{old->new}}` markers
3. Patch application + keep-interval computation in Python
4. Blender VSE auto-layout in headless mode

Final deliverable is a `.blend` project for human editing.

## Pipeline Overview

`scripts/run_pipeline.sh` orchestrates all stages end-to-end. Use `--from-stage N` to skip earlier stages and reuse their outputs.

### Stage 1 — WhisperX Transcription

Speech-to-text with word-level alignment. Runs in a single Docker container for all source files to avoid model reload overhead.

- **Inputs:** source video files (mp4/mkv/mov/avi/webm)
- **Outputs:** `{stem}.json` (word timings), `{stem}.txt` (plain text)

### Stage 2 — Text Editing Checkpoint (mandatory)

Produces `{stem}_edits.txt` for human review. When `stage2.use_llm` is `false` (default), copies the Stage 1 `.txt` as-is. When enabled, runs LLM filter and writes output with `{{old->new}}` markers preserved.

- **Inputs:** `{stem}.txt`
- **Outputs:** `{stem}_edits.txt`

### Stage 3 — Patch Application + Keep-Interval Computation

Applies `{{old->new}}` patches from `_edits.txt`, syncs corrected text back into WhisperX JSON timing data, then runs NLP analysis (GiNZA/spaCy bunsetsu segmentation) to compute keep intervals. Runs per source via `uv run`.

- **Inputs:** `{stem}_edits.txt`, `{stem}.json` (Stage 1 original)
- **Outputs:** `{stem}_intervals.json` (keep intervals + captions)

### Stage 4 — Blender VSE Layout

Auto-assembles the rough cut in headless Blender. References original media in-place (no re-encoding). Concatenates all sources onto a single timeline.

- **Inputs:** source video files, `{stem}_intervals.json` for each source
- **Outputs:** `{stem}_edited.blend` — ready for human editing

### Human Editing Workflow

1. Run stages 1–2 → Stage 2 produces `{stem}_edits.txt`
2. Human edits `_edits.txt` using `{{old->new}}` patch syntax
3. Resume with `--from-stage 3` → applies patches, syncs JSON, computes intervals, runs Stage 4

## Hard Constraints

- Dependency management uses uv + pyproject.toml.
- Runtime NLP dependency is `ginza` + `ja_ginza` (spaCy-based).
- Preserve Stage 3 interval JSON as human-editable contract for Stage 4.
- Stage 4 must reference original media; do not re-encode/copy source media.

## Project Structure

```
config.example.yml            # Documented YAML config template with all defaults
src/nagare_clip/          # Main Python package (src layout)
  config.py                   # Centralised config loading/merging (DEFAULTS dict)
  cli.py                      # Stage 3 CLI entry point
  __main__.py                 # python -m nagare_clip support
  stage2/                     # Stage 2 modules (text editing checkpoint)
    cli.py                    # Stage 2 CLI entry point
    llm_filter.py             # LLM API calls, {{old->new}} patch parsing, apply_patches_to_lines()
  stage3/                     # Stage 3 modules (patch application + intervals)
    sync_json.py              # Sync corrected text back into WhisperX JSON
    bunsetu.py                # Bunsetsu-level timing (GiNZA)
    speech.py                 # Speech span extraction
    intervals.py              # Interval manipulation
    captions.py               # Caption chunking
    filler.py                 # Filler word config (unused at runtime)
    io.py                     # Source file inference
  stage4/                     # Stage 4 modules
    blender_cli.py            # Stage 4 CLI (runs inside Blender)
    scene.py                  # Blender scene setup
    timeline.py               # Strip and caption placement
scripts/
  run_pipeline.sh             # Main orchestrator
tests/
  test_config.py              # Config module unit tests
  stage2/                     # Stage 2 unit tests
  stage3/                     # Stage 3 unit tests
  stage4/                     # Stage 4 tests
```

## Configuration System

All tunable parameters are centralised in `src/nagare_clip/config.py`:

- `DEFAULTS` dict holds the canonical defaults for all sections.
- `get_effective_config(config_path, cli_overrides)` merges DEFAULTS ← config file ← CLI overrides (highest priority wins).
- `config.example.yml` documents every key with its default value; copy it to start a project config.

**Priority order (highest first):** CLI flags > YAML config file > built-in defaults.

Both `cli.py` (Stage 3) and `blender_cli.py` (Stage 4) accept a `--config <path>` flag that is passed through by `scripts/run_pipeline.sh` when `--config` is provided.

`scripts/run_pipeline.sh` also reads `pipeline.*` and `stage1.*` config keys directly via Python/yaml for Docker Compose arguments that are not forwarded to a Python CLI. This includes `stage1.language` (default `ja`).

## Current Runtime Quirks

- Stage 2 is a mandatory text editing checkpoint. When `use_llm: false` (default), copies Stage 1 `.txt` to `_edits.txt`. When `use_llm: true`, runs LLM filter via Ollama native chat API (default: `localhost:11434`) and preserves `{{old->new}}` markers in output. Falls back to original text on any LLM or parse failure. `stage2.thinking` (default `false`) controls thinking mode: accepts `true`/`false` or a string level (`"low"`, `"medium"`, `"high"`) for models that support it (e.g. Qwen 3.5); the value is sent as `"think"` in the API request.
- Stage 3 reads `_edits.txt`, applies `{{old->new}}` patches via `apply_patches_to_lines()`, syncs clean text back into WhisperX JSON via `sync_text_to_json()`, then computes intervals.
- Stage 3 keep intervals are expanded by configurable pre/post margins (defaults 1.0s) and merged before Blender export.
- Stage 3 silence-based keep-interval detection uses WhisperX word timings (`word.start`/`word.end`) with a 0.6s per-word span cap so inflated token ends do not hide pauses. The cap is controlled by `stage3.bunsetu.silence_max_word_span` in the config.
- Stage 3 bunsetsu timing (`build_bunsetu_times` in `src/nagare_clip/stage3/bunsetu.py`) uses `ginza.bunsetu_spans(doc)` so particles and auxiliaries attach to the preceding content word, producing natural subtitle line-break units. It detects large intra-bunsetsu character gaps (> 0.6s) caused by WhisperX misalignment and snaps the bunsetsu start forward to the later character cluster so the silence gap is not hidden inside a single bunsetsu. The gap threshold is `stage3.bunsetu.silence_max_word_span`; the end-offset epsilon is `stage3.bunsetu.char_eps`.
- Stage 3 captions are chunked on GiNZA bunsetsu units with bunsetsu-level timing (`end = min(start+char_eps, next start)`), split on silence gaps and keep-boundary crossings; defaults are 12 bunsetsu, 4.0 seconds max, minimum 3 bunsetsu, min duration 1.5s, and silence flush at 1.5s. Bunsetsu units within a chunk are joined with a configurable separator (default `' '`, controlled by `stage3.caption.bunsetu_separator`); a space between units enables word-wrap in Blender TEXT strips.
- Stage 3 captions are preserved as transcript chunks (not pre-filtered by keep intervals), and Stage 3 expands keep intervals to include caption spans so subtitles are retained in Stage 4.
- After caption-based expansion, Stage 3 re-applies `stage3.min_keep` so tiny keep strips are expanded/merged when possible.
- Stage 4 caption style (font size, alignment, position, shadow) is controlled by `stage4.caption_style.*` in the config.
- Stage 4 fallback FPS (used when source metadata is unavailable) is controlled by `stage4.default_fps`.
- Stage 4 supports multiple source files: `blender_cli.py` accepts repeated `--source`/`--intervals` flags; `place_strips()` and `build_timeline_map()` accept `start_cursor` and `idx_offset` to concatenate sources on a single timeline.
- `run_pipeline.sh` discovers all video files (`mp4`, `mkv`, `mov`, `avi`, `webm`) in `INPUT_VIDEOS_DIR` alphabetically when `--source` is not provided. Multiple `--source` flags are also accepted.
- `run_pipeline.sh` accepts `--from-stage N` (1, 2, 3, or 4) to skip expensive earlier stages and reuse their outputs. Also configurable via `pipeline.from_stage` in YAML config. When skipping stages, the script validates that required intermediate outputs exist.
- Stage 1 (WhisperX) runs in a **single container** for all source files, passing all relative paths as positional arguments. This avoids model reload overhead between videos. Stage 3 still loops per-source after the single Stage 1 completes.

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
