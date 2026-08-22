# Observability — runtime notes

Two independent sinks record every LLM call: a human-readable markdown report
(`llm_report`) and optional Langfuse OTEL tracing. Both hang off the single LLM
chokepoint, `nagare_clip.llm_client.call_llm`.

## LLM report (markdown)

- All LLM stages record every attempt (prompt, raw response, retries, outcome, plus call config like `model`/`thinking`) via `llm_report.Recorder` into `output/llm_report/` (config `general.llm_report`/`llm_report_dir`); the pipeline orchestrator's per-stage adapter (`nagare_clip.pipeline.stages`) builds one recorder per stage and clears that stage's subdir **once per run** (before its stem loop, not once per source), passes the recorder through to the stage's typed `run()` function (default `NULL_RECORDER` keeps functions testable), and rebuilds `index.md` from front-matter after the loop. This clear-once-per-run happens uniformly for every LLM stage regardless of whether the stage's own LLM usage is enabled — e.g. `text_filter`'s subdir is cleared even when `text_filter.use_llm` is `false`, unlike the old per-stage CLI, which only created/cleared the recorder when `use_llm` was true. Outcomes: ok / ok-empty / llm-error / unparseable / verify-fail / dropped-items (the last surfaces previously-silent per-item drop warnings with counts). `llm_report.STAGE_ORDER` (a display/sort order for `index.md`, independent of the pipeline's own stage order) includes every LLM stage, `gap_context` and `publish` among them.
- Unit front-matter carries `started_at`/`duration_ms`. The duration is measured from `Recorder.begin(unit)` — called at each unit's start, before its first LLM call (every stage pairs it with its `with_trace_meta(...)` line) — to `flush_unit()`. Stages record an `attempt()` only **after** a call returns, so without `begin()` the start time defaulted to first-attempt time and every report said `duration_ms: 0`. `begin()` is idempotent per unit (`setdefault`) and a no-op on a disabled recorder.
- `text_filter` report units are stem-prefixed (`<stem> lines 5-8 (size 4)` → `<stem>_lines_5-8_size_4.md`): every video restarts its line numbering at 1, so un-prefixed units from a multi-video run collided on the same report file and silently overwrote each other (`filter_transcript(..., stem=...)`, passed by `run_text_filter` from the txt basename; empty stem keeps the old name for direct callers).
- `gap_context` is the one multimodal LLM stage: its per-call user message embeds each frame as a base64 `data:image/...;base64,...` `image_url` part. The recorder never sees that payload — `gap_context.describe._report_messages()` flattens the recorded message to the same text header plus a `Frames:\n- <relpath>` list before handing it to `recorder.attempt()`, so `output/llm_report/gap_context/<unit>.md` holds frame **paths**, never base64 (`tests/gap_context/test_describe.py::test_describe_gap_recorder_excludes_base64_payloads` pins this) — keeping report files small, diffable, and free of binary blobs.
- Span ops in guided_edit are applied deterministically (no LLM call); their records render as `deterministic — <outcome>` with no temperature, and never contribute a model name to the unit front-matter.

## Deterministic notes (`llm_report/notes/`)

`rebuild_index()` inlines every non-empty `notes/*.md` at the bottom of
`index.md`, in filename order. They live in `notes/` rather than a stage subdir
precisely so a later stage's index rebuild does not clear them, and each is
produced with **no LLM call**:

| file | written by | covers |
|---|---|---|
| `plan_divergence.md` | `pipeline.stages._write_divergence_note` after `director` | directions the ops that landed argue with ([plan.md](plan.md)) |
| `cut_report.md` | `pipeline.stages.write_cut_report` after `intervals`, again after `blender` | the finished cut's measurements + threshold breaches ([cut_report.md](cut_report.md)) |

Both follow the same rule: state the number that was breached, so the threshold
is arguable rather than hidden. An empty result deletes a stale note.

## Langfuse tracing

- `call_llm` (the single LLM chokepoint) registers LiteLLM's `langfuse_otel` callback exactly once per process via `_ensure_tracing()` and attaches per-call grouping metadata when tracing is enabled: `generation_name="<stage>/<unit>"`, `tags=["stage:<stage>","stem:<unit>"]`, `session_id=NAGARE_RUN_ID`. Metadata is carried via `with_trace_meta(cfg, stage=..., unit=...)` under a reserved `cfg["_trace"]` key that `call_llm` pops before passing kwargs to the provider — so the provider call is byte-identical when tracing is disabled. Tracing is enabled iff `LANGFUSE_PUBLIC_KEY` **and** `LANGFUSE_SECRET_KEY` are set in the environment **and** `NAGARE_LANGFUSE != "0"` **and** `general.langfuse` (config, default `true`) is not false; the pipeline CLI (`nagare_clip.pipeline.cli`) sets `NAGARE_RUN_ID` (one timestamp per run) and maps `general.langfuse: false` to `NAGARE_LANGFUSE=0` once, in-process, before any stage runs — since the whole pipeline is a single Python process, every in-process stage sees the same environment automatically, and the Blender subprocess (the only remaining external process besides Docker) inherits it via the child environment. `LANGFUSE_OTEL_HOST` selects region / self-hosted endpoint (default: US cloud; EU: `https://cloud.langfuse.com`). Pending spans are flushed via an `atexit` hook (`flush_traces()` calls `provider.force_flush()` best-effort, never raises) that now fires once per pipeline run instead of once per short-lived stage CLI; if OTEL flushing proves unreliable the documented fallback is to switch the callback to the langfuse-SDK integration (`["langfuse"]`) and call `langfuse.flush()` in `flush_traces`. The markdown `llm_report` runs alongside as an independent sink (untouched). This is observability-only and decoupled from any future LangGraph migration.

## Logging

- `setup_logging` caps the `LiteLLM*`/`httpx` loggers at WARNING and drops the known-noise 'Proxy Server is not installed' record; set `general.log_level: DEBUG` to see LiteLLM's full chatter again.
