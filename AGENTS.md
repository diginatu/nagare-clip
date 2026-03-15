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
