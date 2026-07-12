# Pipeline orchestration — runtime notes

`scripts/run_pipeline.sh` is a 6-line shim: `exec uv run --project "$PROJECT_ROOT"
python -m nagare_clip.pipeline "$@"`. All orchestration logic lives in the Python
package `src/nagare_clip/pipeline/`. See the
[Pipeline Overview in AGENTS.md](../../AGENTS.md#pipeline-overview) for the
per-stage input/output contract; this file covers the orchestrator's own
runtime behavior.

## Modules

- `errors.py` — `PipelineError`, the single user-facing failure type. `cli.main()`
  catches it, prints the message to stderr, and returns exit code 1 (no
  traceback for expected failures like a bad `--from-stage` name or a missing
  source file).
- `sources.py` — `discover_sources()` finds `mp4/mkv/mov/avi/webm` files directly
  inside the input dir, sorted by name, raising `PipelineError` if none are
  found; `resolve_cli_sources()` resolves explicit `--source` values (bare
  names live under the input dir, paths containing `/` are used as-is) and
  raises if a file is missing. `stage_sources()` then makes every source
  reachable inside the input dir for Docker: a source already inside the input
  dir is referenced in place, one outside is copied in (returned in a cleanup
  list the CLI unlinks after the run) — but `SourceMedia.abs_path` always keeps
  the *original* path, so the blender stage still references original media,
  never the staged copy.
- `external.py` — the only two subprocess boundaries in the whole pipeline:
  the whisperx Docker image (WhisperX transcription, ffmpeg `silencedetect`)
  and headless Blender. `build_transcription_cmd()`, `build_silencedetect_cmd()`,
  and `build_blender_cmd()` are pure functions returning `list[str]` command
  lines (testable without Docker/Blender installed); `run_command()` actually
  runs one via `subprocess.run(..., check=True)`, optionally merging extra env
  vars and redirecting stderr to a file (used to capture `silencedetect`'s
  stderr for the audio_silence stage). Every other stage runs in-process — no
  subprocess, no re-parsing `--config`.
- `runner.py` — `Stage` (name, `run(ctx)` callable, optional
  `required_outputs(ctx)` callable) and `PipelineContext` (cfg, paths, resolved
  `SourceMedia` list, `from_index`/`to_index`) are the generic engine.
  `resolve_window(stages, from_stage, to_stage)` raises `PipelineError` for an
  unknown stage name or an inverted range (`from` after `to`) — stages are
  looked up by name in the registry list, so **any unrecognized name is now a
  clean, immediate error** (the old bash `stage_index` quirk, which returned
  success even for a typo'd name and let `set -e` limp to a separate error
  path, is gone). `run_stages()` walks the registry once: stages inside the
  `[from_index, to_index]` window run; stages after `to_index` are skipped with
  a log line and no validation (they're intentionally not built yet); stages
  before `from_index` are skipped but each of their `required_outputs(ctx)`
  paths must already exist, else `PipelineError` ("Missing output ... required
  when skipping ..."). Any stage exception other than `PipelineError` is
  wrapped as `PipelineError(f"[{stage.name}] failed: {exc}")` so the CLI's
  top-level handler is the only place that prints and exits non-zero.
- `stages.py` — `STAGE_NAMES` (the ten canonical stage names, in order) and
  `STAGES` (the `Stage` registry consumed by `runner.run_stages`). One adapter
  function per stage translates `PipelineContext` into that stage's typed
  `run()` call (`run_audio_silence`, `run_sentence_split`, `run_summary`,
  `run_text_filter`, `run_plan`, `run_director`, `run_guided_edit`, `run_intervals`)
  or an external command (`transcription`, `blender`). `transcription`,
  `summary`, `plan`, and `blender` run once per pipeline invocation;
  `audio_silence`, `sentence_split`, `text_filter`, `director`, `guided_edit`,
  and `intervals` loop per source inside their adapter. Each LLM stage adapter
  (`sentence_split`, `summary`, `text_filter`, `plan`, `director`,
  `guided_edit`) owns its `llm_report.Recorder` lifecycle: it builds one
  recorder via `recorder_from_config(stage, cfg, override_dir=...)`, calls
  `rec.clear()` **once before** its stem loop (so a stage's `output/llm_report/`
  subdir is cleared exactly once per run, not once per source), then
  `rec.rebuild_index()` in a `finally` after the loop.
- `cli.py` — `argparse` flags mirror the historical bash script
  (`--source` repeatable, `--config`, `--language`, `--input-videos-dir`,
  `--output-dir`, `--keep-pre-margin`, `--keep-post-margin`, `--from-stage`,
  `--to-stage`, `--align-model`). Explicit CLI values are folded into a
  `cli_overrides` dict (`build_cli_overrides`) and passed to
  `get_effective_config(config_path, cli_overrides)`, so precedence is
  **CLI > YAML > model defaults**, validated the same way as every other
  config consumer. `main()` then: creates the input/output/per-stage/cache
  directories, calls `setup_logging()` once, discovers or resolves sources and
  stages them, resolves the stage window, builds the `PipelineContext`, and
  runs `run_stages`. Source-staging cleanup (`stage_sources`'s copied-in files)
  happens in a `finally` regardless of success. `PipelineError` is the only
  exception type caught at this level.
- `__main__.py` — `python -m nagare_clip.pipeline` entry point (`sys.exit(main())`).
  The top-level `src/nagare_clip/__main__.py` re-aliases `python -m nagare_clip`
  to the same `nagare_clip.pipeline.cli.main` (previously it ran only the
  intervals stage).

## Stage-name validation and source discovery order

Stage-name validation (`resolve_window`) happens **after** source discovery in
`cli.main()` — discovering/resolving sources runs first, so an invalid
`--from-stage`/`--to-stage` name is only reported once at least one video is
found (or resolvable) in the input directory; with an empty input directory
you'll see the "No video files found" `PipelineError` instead.

## Environment: `NAGARE_RUN_ID` / `NAGARE_LANGFUSE`

The pipeline CLI is a **single process** for the whole run (unlike the old
bash script, which was itself the top-level process spawning per-stage CLI
subprocesses). `cli.main()` sets `NAGARE_RUN_ID` once via
`os.environ.setdefault(...)` (one timestamp groups every LLM call in the run
under one Langfuse session) and maps `general.langfuse: false` to
`NAGARE_LANGFUSE=0`, both before any stage runs. Because everything except
Docker/Blender executes in-process, this is set exactly once — no propagation
step is needed for in-process stages. The Blender subprocess (`external.py`'s
`build_blender_cmd`/`run_command`) inherits the parent's environment via
`os.environ` merged into `subprocess.run`, so it sees the same
`NAGARE_RUN_ID`/`NAGARE_LANGFUSE`. Being single-process also means the
`atexit`-registered Langfuse flush (`flush_traces()`) fires exactly once, at
interpreter exit, instead of once per stage subprocess under the old script.

## Output layout

Output dirs are one per stage by name, created up front by `cli.main()`:
`output/transcription|audio_silence|sentence_split|summary|text_filter|plan|director|guided_edit|intervals|blender/`.
`sentence_split`/`summary`/`plan`/`director`/`guided_edit` all still run
unconditionally (cheap no-ops when their stage is disabled in config), exactly
as under the bash orchestrator — see [AGENTS.md](../../AGENTS.md#pipeline-overview)
for what each no-op produces.
