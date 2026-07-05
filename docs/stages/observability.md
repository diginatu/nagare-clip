# Observability — runtime notes

Two independent sinks record every LLM call: a human-readable markdown report
(`llm_report`) and optional Langfuse OTEL tracing. Both hang off the single LLM
chokepoint, `nagare_clip.llm_client.call_llm`.

## LLM report (markdown)

- All LLM stages record every attempt (prompt, raw response, retries, outcome, plus call config like `model`/`thinking`) via `llm_report.Recorder` into `output/llm_report/` (config `general.llm_report`/`llm_report_dir`); each stage CLI clears its own subdir, passes the recorder through (default `NULL_RECORDER` keeps functions testable), and rebuilds `index.md` from front-matter. Outcomes: ok / ok-empty / llm-error / unparseable / verify-fail / dropped-items (the last surfaces previously-silent per-item drop warnings with counts).

## Langfuse tracing

- `call_llm` (the single LLM chokepoint) registers LiteLLM's `langfuse_otel` callback exactly once per process via `_ensure_tracing()` and attaches per-call grouping metadata when tracing is enabled: `generation_name="<stage>/<unit>"`, `tags=["stage:<stage>","stem:<unit>"]`, `session_id=NAGARE_RUN_ID`. Metadata is carried via `with_trace_meta(cfg, stage=..., unit=...)` under a reserved `cfg["_trace"]` key that `call_llm` pops before passing kwargs to the provider — so the provider call is byte-identical when tracing is disabled. Tracing is enabled iff `LANGFUSE_PUBLIC_KEY` **and** `LANGFUSE_SECRET_KEY` are set in the environment **and** `NAGARE_LANGFUSE != "0"` **and** `general.langfuse` (config, default `true`) is not false; `run_pipeline.sh` exports `NAGARE_RUN_ID` (one timestamp per run) and maps `general.langfuse: false` to `export NAGARE_LANGFUSE=0` for all subprocesses. `LANGFUSE_OTEL_HOST` selects region / self-hosted endpoint (default: US cloud; EU: `https://cloud.langfuse.com`). Short-lived stage CLIs flush pending spans via an `atexit` hook (`flush_traces()` calls `provider.force_flush()` best-effort, never raises); if OTEL flushing proves unreliable the documented fallback is to switch the callback to the langfuse-SDK integration (`["langfuse"]`) and call `langfuse.flush()` in `flush_traces`. The markdown `llm_report` runs alongside as an independent sink (untouched). This is observability-only and decoupled from any future LangGraph migration.
