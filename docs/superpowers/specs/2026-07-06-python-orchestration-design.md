# Python pipeline orchestration — design

Date: 2026-07-06
Status: approved for planning

## Problem

`scripts/run_pipeline.sh` (633 lines) owns all pipeline orchestration. It
re-implements config precedence in bash (`CFG_*`/`CLI_*` variable pairs for
every option), shells into a Python heredoc to read YAML (unvalidated, with
inline defaults that must be kept in sync with the pydantic models by hand),
and carries fragile idioms documented as quirks (e.g. `stage_index`
deliberately returning success for unknown names to survive `set -e`). None
of it is unit-testable.

## Goal

Port orchestration to a `nagare_clip.pipeline` package: stage windowing,
config resolution, source discovery, and per-source looping in unit-testable
Python. Subprocesses remain only where a process boundary genuinely exists:
Docker (WhisperX, ffmpeg silencedetect) and Blender.

## Decisions (settled during brainstorming)

1. **Entry point**: `scripts/run_pipeline.sh` becomes a ~3-line shim that
   `exec`s `uv run --project "$ROOT" python -m nagare_clip.pipeline "$@"`.
   Docs and muscle memory keep working; deletable later.
2. **Stage invocation**: in-process. The orchestrator imports each stage and
   calls a typed `run()` function — no `python -m <stage>.cli` subprocesses.
3. **Stage API depth**: full refactor. Every stage exposes a typed `run()`
   function; the per-stage CLIs are **deleted** (see below), not kept as
   wrappers.
4. **Parity**: same flags and behaviors as the bash script; small fixes
   allowed where bash was a workaround (argparse-native errors, single config
   load). Output wording matches in shape, not byte-for-byte.
5. **Architecture**: declarative stage registry + generic runner (approach A;
   procedural port and DAG framework rejected).

## Architecture

New package `src/nagare_clip/pipeline/`:

| Module | Responsibility |
|---|---|
| `stages.py` | `Stage` dataclass + ordered `STAGES` registry |
| `runner.py` | Window resolution, skip-validation, per-source looping, `PipelineContext` |
| `sources.py` | Video discovery, `--source` resolution, copy-in/cleanup of out-of-dir sources |
| `external.py` | Pure arg-builders + thin subprocess wrappers for `docker compose` and `blender` |
| `cli.py` | argparse (same flags as bash), CLI overrides, env setup, calls runner |
| `__main__.py` | `python -m nagare_clip.pipeline` |

### Stage registry (`stages.py`)

`Stage` declares:

- `name` — canonical stage name (the only identifier; no numbers, honoring
  the insert-anywhere convention).
- scope — `per_source` (runs once per stem) or `project_wide` (runs once).
- `run` — a callable receiving the `PipelineContext` (plus the source index
  for per-source stages) that adapts context paths into the stage's typed
  `run()` function or external command.
- `required_outputs(ctx) -> list[Path]` — files that must exist when the
  stage is skipped via `--from-stage`. Transcription's conditional rule
  (its outputs are only required when `sentence_split` will actually run,
  i.e. `from <= sentence_split`) is expressed inside its callable, not as a
  runner special case.

`STAGES` is the single ordered list:
`transcription, audio_silence, sentence_split, text_filter, summary, plan,
director, guided_edit, intervals, blender`.

### Runner (`runner.py`)

- Resolves `--from-stage`/`--to-stage` names to indices; unknown name or
  `from > to` → friendly error, exit 1 (replaces the `stage_index` set-e
  quirk).
- One loop over `STAGES`: in-window → run (looping stems for per-source
  stages, printing `[name] …` progress lines); past window → `[name] Skipped
  (--to-stage X)`; before window → `[name] Skipped (--from-stage X)` +
  validate `required_outputs`, erroring on missing files.
- `PipelineContext` carries: effective config dict, project root, resolved
  input/output dirs, per-stage output dirs, sources/stems/relatives,
  intervals-json paths, log file, LLM report dir, cleanup list.
- Cleanup of copied-in sources always runs (try/finally), matching bash's
  end-of-script cleanup.
- Any exception in a stage aborts the run with a stage-named error message
  and exit code 1 (parity with `set -e`).

### External processes (`external.py`)

Only two process boundaries remain:

- **Docker**: `docker compose -f <root>/docker-compose.yml run --rm --user 0:0
  whisperx …` for transcription (single container run for all sources, as
  today) and `--entrypoint ffmpeg … silencedetect …` per source (stderr
  captured to `{stem}_silencedetect.log`). `INPUT_VIDEOS_DIR`/`OUTPUT_DIR`
  passed via the subprocess env.
- **Blender**: `blender --background --factory-startup --python-exit-code 1
  --python …/blender_cli.py -- …`. Because Blender is a separate process, it
  still receives `--config <path>` and `--log-file` on its command line —
  the in-process config dict cannot cross that boundary.

Arg construction is pure (list-of-strings builders) so it is testable
without Docker or Blender installed.

## Stage `run()` functions; per-stage CLIs deleted

Each stage's `cli.py` is replaced by a `run.py` exposing one typed function,
e.g.:

```python
def run_director(
    edits_txt: Path,
    output: Path,
    cfg: dict,
    *,
    summary: Path | None,
    plan: Path | None,
    stem: str,
    json_path: Path | None,
    recorder: Recorder = NULL_RECORDER,
) -> None: ...
```

(Exact signatures per stage are fixed in the implementation plan; the
convention is: required inputs/outputs positional, optional artifacts
keyword-only, `cfg` is the full effective-config dict, LLM stages take a
`recorder`.)

Deleted: `parse_args`/`main` in `audio_silence`, `sentence_split`,
`text_filter`, `summary`, `plan`, `director`, `guided_edit`, `intervals`
CLIs, including the `--llm-report-no-clear` flag. Standalone single-stage
runs are covered by `--from-stage X --to-stage X`.

Kept:

- `blender/blender_cli.py` — runs inside Blender's bundled Python; must stay
  a script.
- `intervals/check_edits.py` — documented human-facing `_edits.txt`
  validator, untouched.
- `nagare_clip/__main__.py` — repointed: `python -m nagare_clip` becomes an
  alias for the pipeline CLI (it currently runs the intervals stage).

### Cross-cutting concerns (moved to the orchestrator, once per run)

- **Config**: `get_effective_config(config_path, cli_overrides)` loaded once;
  stage `run()`s receive the dict. The heredoc YAML read disappears; the
  "Known residual" in AGENTS.md is resolved.
- **Logging**: `setup_logging` once; log file defaults to
  `output/pipeline.log` as today.
- **LLM report**: orchestrator creates one `Recorder` per LLM stage and
  clears it once before that stage's stem loop (replacing the bash
  `REPORT_CLEARED_*` / `--llm-report-no-clear` dance). `rebuild_index()`
  after each stage completes.
- **Env**: `NAGARE_RUN_ID` set once at startup (timestamp, unless inherited);
  `general.langfuse: false` → `NAGARE_LANGFUSE=0` in the process env
  (inherited by the Blender subprocess). Single process → the `atexit`
  Langfuse flush fires once at the end of the run.
- **Accepted in-process consequence**: GiNZA/spaCy models stay resident
  across stages instead of being released at stage-process exit.

## CLI surface

Flags (unchanged from bash): `--source` (repeatable), `--config`,
`--language`, `--input-videos-dir`, `--output-dir`, `--keep-pre-margin`,
`--keep-post-margin`, `--from-stage`, `--to-stage`, `--align-model`.

- Explicit CLI values map into `cli_overrides` for `get_effective_config`
  (`transcription.language`, `pipeline.input_videos_dir`, `pipeline.output_dir`,
  `intervals.keep_pre_margin`, `intervals.keep_post_margin`,
  `pipeline.from_stage`, `pipeline.to_stage`, `transcription.align_model`),
  so CLI > YAML > defaults is enforced by the validated config system.
- Behavior preserved: source auto-discovery (mp4/mkv/mov/avi/webm,
  top-level of input dir, sorted; error when none), bare `--source` name →
  under input dir, out-of-dir source copy-in + cleanup, Japanese default
  align model `vumichien/wav2vec2-large-xlsr-japanese`, output dirs created
  upfront (incl. `cache/`), `{first_stem}_edited.blend` naming, final
  `Done: …` / `Done (stopped at --to-stage X)` line.
- Small fixes: argparse-native `--help` and unknown-flag errors; the bash
  script's dead `CLI_SILENCE_THRESHOLD`/`CLI_MIN_KEEP` variables (no flag
  ever set them) are dropped, not ported.

## Testing

TDD throughout (red before green).

- **New unit tests**: window resolution (unknown names, inverted range);
  source discovery/resolution/copy-in + cleanup; skip-validation incl.
  transcription's conditional; runner driving a fake `STAGES` registry
  (order, scoping, skip messages, abort-on-failure); docker/blender
  arg-builders.
- **Stage tests**: each deleted CLI's tests are rewritten
  scenario-for-scenario against the new `run()` functions (no subprocess);
  pure-function tests untouched.
- **Pipeline CLI test**: flag parsing and override precedence with external
  calls stubbed.
- `make check` (ruff, format, validate, pytest) green before finishing. CI's
  `bash -n scripts/run_pipeline.sh` still passes on the shim.

## Documentation updates

- `README.md` — user-facing usage (entry point unchanged via shim; note the
  Python CLI).
- `AGENTS.md` — project structure (new `pipeline/` package, per-stage
  `run.py` replacing `cli.py`), pipeline description, delete the
  "Known residual: run_pipeline.sh reads raw YAML" note and the
  `--config` pass-through paragraph's stage-CLI references.
- `docs/stages/pipeline.md` — rewritten for the Python orchestrator.
- `config.example.yml` — regenerate only if models change (none expected).

## Out of scope

- No new config keys or model changes.
- No change to any stage's algorithmic behavior, file formats, or the
  interval JSON contract.
- No re-encoding/media handling changes; Blender still references originals.
- No console-script entry point in `pyproject.toml` (can be added later).
