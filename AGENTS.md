# AGENTS.md

Agent guidance for this repository.

## Objective

Maintain and improve a 3-stage rough-cut pipeline:

1. WhisperX transcription in Docker
2. Keep-interval computation in Python
3. Blender VSE auto-layout in headless mode

Final deliverable is a `.blend` project for human editing.

## Hard Constraints

- No diarization flow in this project.
  - Do not add `--diarize`.
  - Do not add `--hf_token` handling.
  - Do not add diarization-specific dependencies.
- Dependency management uses uv + pyproject.toml. Always run `uv lock` after changing dependencies and commit uv.lock.
- Preserve Stage 2 interval JSON as human-editable contract for Stage 3.
- Stage 3 must reference original media; do not re-encode/copy source media.

## Current Runtime Quirks (Do Not Regress)

- Stage 1 in `run_pipeline.sh` currently uses:
  - `--user "0:0"`
  - a leading `_` positional argument before source filename
- The selected WhisperX image tag does not accept `--word_timestamps`.
- Blender execution should keep:
  - `--factory-startup`
  - `--python-exit-code 1`
- Blender VSE API compatibility:
  - prefer `sequence_editor.sequences`
  - fallback to `sequence_editor.strips`
- Stage 2 keep intervals are expanded by configurable pre/post margins (defaults 1.0s) and merged before Blender export.
- Stage 2 silence-based keep-interval detection uses WhisperX word timings (`word.start`/`word.end`) with a 0.6s per-word span cap so inflated token ends do not hide pauses.
- Stage 2 captions are chunked on `fugashi` morphemes using `fugashi[unidic-lite]` with morpheme-level timing (`end = min(start+0.02s, next start)`), split on silence gaps and keep-boundary crossings; defaults are 12 morphemes, 4.0 seconds max, minimum 3 morphemes, min duration 1.5s, and silence flush at 1.5s.
- Stage 2 captions are preserved as transcript chunks (not pre-filtered by keep intervals); Stage 3 is responsible for mapping captions onto overlapping keep intervals.
- Changes to `run_pipeline.sh` or `docker-compose.yml` must preserve:
  - default `INPUT_VIDEOS_DIR=src_video`, `OUTPUT_DIR=output`
  - Docker mounts driven by the same env vars as the shell script
  - explicit source paths (containing `/`) continue to work without rewriting

## Preferred Validation

Validation runs automatically via the OpenCode `PostToolUse` hook
defined in `.opencode/config.json`. It triggers after every file
write/edit and runs:

- `docker compose config --services`
- `python -m py_compile stage2_intervals.py stage3_blender.py`
- `bash -n run_pipeline.sh`

If environment allows, also validate with a full run:

```bash
./run_pipeline.sh "input/<sample>.mp4" ja
```

## Documentation Policy

When behavior changes, update all of:

- `README.md` (user-facing usage)
- `plan.md` (implementation/status)
- this `AGENTS.md` (agent guardrails)
