# Pipeline orchestration — runtime notes

`scripts/run_pipeline.sh` is a 6-line shim: `exec uv run --project "$PROJECT_ROOT"
python -m nagare_clip.pipeline "$@"`. All orchestration logic lives in the Python
package `src/nagare_clip/pipeline/`. See the
[Pipeline Overview in AGENTS.md](../../AGENTS.md#pipeline-overview) for the
per-stage input/output contract; this file covers the orchestrator's own
runtime behavior.

## Modules

- `errors.py` — `PipelineStop`, a deliberate stop that is not a failure (the director's `pause_after_plan`): `cli.main()` prints it to stdout and returns 0, and `run_stages` re-raises it unwrapped. `PipelineError`, the single user-facing failure type. `cli.main()`
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
  the whisperx Docker image (WhisperX transcription, ffmpeg `silencedetect`,
  and the gap_context stage's frame snapshots) and headless Blender.
  `build_transcription_cmd()`, `build_silencedetect_cmd()`,
  `build_snapshot_batch_cmd()`, and `build_blender_cmd()` are pure functions
  returning `list[str]` command lines (testable without Docker/Blender
  installed); `run_command()` actually runs one via `subprocess.run(...,
  check=True)`, optionally merging extra env vars and redirecting stderr to a
  file (used to capture `silencedetect`'s stderr for the audio_silence stage).
  `build_snapshot_batch_cmd()` renders every gap-context frame job (across
  every gap of every source) as one `ffmpeg ... || true` line in a shell
  script run inside a single `--entrypoint sh whisperx -c "<script>"`
  container — container-startup overhead (~0.82s/run) dwarfs the ~30ms
  ffmpeg cost per frame, so `pipeline/stages.py::_extract_gap_frames` issues
  exactly one `run_command()` call for the whole stage instead of one per
  frame (see [`docs/stages/gap_context.md`](gap_context.md) for the measured
  numbers). Every other stage runs in-process — no subprocess, no
  re-parsing `--config`.
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
  when skipping ..."). Any stage exception other than `PipelineError`/`PipelineStop` is
  wrapped as `PipelineError(f"[{stage.name}] failed: {exc}")` so the CLI's
  top-level handler is the only place that prints and exits non-zero.
- `stages.py` — `STAGE_NAMES` (the thirteen canonical stage names, in order) and
  `STAGES` (the `Stage` registry consumed by `runner.run_stages`). One adapter
  function per stage translates `PipelineContext` into that stage's typed
  `run()` call (`run_audio_silence`, `run_sentence_split`, `run_gap_context`,
  `run_summary`, `run_text_filter`, `run_plan`, `run_plan_revise`, `run_director`,
  `run_guided_edit`, `run_intervals`, `run_publish`) or an external command
  (`transcription`, `blender`). `transcription`, `summary`, `plan`, `plan_revise`, `blender`
  and `publish` run once per pipeline invocation; `audio_silence`,
  `sentence_split`, `gap_context`, `text_filter`, `director`, `guided_edit`,
  and `intervals` loop per source inside their adapter. `publish` runs once
  but still reads per-source material (its frame shortlist and caption list),
  which it gathers in the adapter before a single batched Docker call — the
  same shape as `gap_context`'s extraction. Each LLM stage adapter
  (`sentence_split`, `gap_context`, `summary`, `text_filter`, `plan`,
  `plan_revise`, `director`, `guided_edit`, `publish`) owns its `llm_report.Recorder`
  lifecycle: it builds one
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

`sources.project_stems(input_dir)` is a second, non-raising read of that same
discovery: the stems of every video in the input directory, in name order — the
order the `blender` stage concatenates, since `_blender_run` just hands it
`ctx.sources`. It exists because `ctx.sources` answers "what is this run
processing", which `--source` narrows, while the `director` stage needs "what
does the finished video contain", which `--source` does not change. So
`_director_run` positions each video with `project_stems(...) or ctx.stems` and
passes the earlier videos' `_director.json` paths alongside; a `--source b.mp4
--from-stage director` re-run therefore still sees `video 2 of 3` and videos
1's captions. An input directory holding videos outside the project would
mis-state the count — but it is exactly the set a full run would concatenate,
so the two never disagree with each other.

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
`output/transcription|audio_silence|sentence_split|gap_context|summary|text_filter|plan|plan_revise|director|guided_edit|intervals|blender|publish/`.
`sentence_split`/`gap_context`/`summary`/`plan`/`plan_revise`/`director`/`guided_edit`/`publish` all still run
unconditionally (cheap no-ops when their stage is disabled in config), exactly
as under the bash orchestrator — see [AGENTS.md](../../AGENTS.md#pipeline-overview)
for what each no-op produces.

## `output/index.md` — the page at the top

`cli.main()` ends with a `finally` around `run_stages()` that calls
`index_page.write_index(ctx.output_dir, cfg, blend=...)`. Four properties are
the whole design:

- **It runs whatever the range was**, including one that produced nothing, and
  **it runs when the pipeline failed** — a run that died in `director` is when
  knowing what is on disk is worth most. It never changes the exit code.
- **It cannot fail the run.** Its own exception is logged and swallowed at the
  call site: a `finally` that raises replaces the real error with its own.
- **It goes last**, after `llm_report/index.md` has been written, because it
  counts that file's calls.
- **It is deliberately not a stage.** It cannot be `STAGES[-1]` (`resolve_window()`
  cuts the list, so `--to-stage render` would drop it), and it needs no
  `Stage.when`/`ALWAYS` concept in the runner: it has one consumer, and the
  `finally` regenerates the page on every invocation, so there is nothing to
  name with `--from-stage`. If a second always-runs step ever appears, build the
  concept then.

The file rows are in pipeline-flow order (`index_page._rows()`, pinned by a
test): `plan_dialogue/history.md` (plan/plan_revise), the `.blend`,
`publish.md`, `render.md`, and `llm_report/index.md` last because it covers
every call of the run rather than one stage. `cut_report.md` deliberately has
no row: `llm_report/index.md` inlines it (its `## finished cut` section) and the
headline already states its source → finished minutes, so a row would be a
duplicate link.

`index_page.py` reads only what is on disk (mtimes, `publish.json`,
`render.json`, `llm_report/index.md`, the intervals JSONs + `timeline.json`) and
sits at the package top level rather than under a stage dir, like `order_note.py`.
The headline is `cut_report.metrics.measure()`, so the page and the cut report
cannot disagree; the finished cut it measures is the **manifest's**, over the
whole project rather than one run's `--source` filter. Nothing under it imports
`llm_client` — zero calls is a property of the code, enforced by an AST check
and a fresh-interpreter run, the way improvement 25 enforced it for `render`.

The embedded thumbnails carry each set's hook in the **alt text**
(`_thumbnail_alt`, hooks read from `publish.json`'s `thumbnail_copy`, no call).
A visible caption line was rejected: the copy is burned into the image, so it is
already in front of the human, and a line under it is redundant to exactly the
reader who can see. Alt is invisible to that reader and is the only description
that reaches a screen reader or a model reading the file, so that is where the
hook goes. `markdown.embed_image` carries it in both markups (it had been
dropping it in the `<img>`), escaping it so it cannot break out of the attribute
or close the image early.

A row states an mtime or `—`, and **never explains an absence**. Two attempts at
diagnosing one missing file while this page was being designed were both wrong
(a partial re-run; a silent failure — in fact the report post-dated that
project's last `intervals` run). A timestamp is a fact; an explanation is a guess
by a writer who cannot see the run.

## The segment order

`pipeline/stages.py` owns the one point every stage consults for the finished
video's playback order: `_timeline_segments(ctx)` (validate + normalise
`director/order.json`, else the effective plan's `order`, else shooting order
from `project_stems`). `_line_counts`, `_write_manifest`, `_ordered_sources` and
`write_order_note` sit beside it. The `director` does **not** read it for its
view — its view is always shooting order — only for its seed
(`_resolve_order(ctx, director=False)`, which skips its own `order.json`), and
writes `director/order.json` itself.

See [`order.md`](order.md).
