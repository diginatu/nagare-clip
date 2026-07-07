# nagare-clip — Implementation Status

## Python Pipeline Orchestration

**Status: complete** (2026-07-07; branch `worktree-python-orchestration`; design in `docs/superpowers/` sdd task specs).

`scripts/run_pipeline.sh` (633 lines of bash) was ported to a Python package,
`src/nagare_clip/pipeline/` — `errors.py` (`PipelineError`), `sources.py`
(source discovery/resolution/staging), `external.py` (docker/blender command
builders + `run_command()`, the only remaining subprocesses), `runner.py`
(`Stage`, `PipelineContext`, `resolve_window()`, `run_stages()` — windowing and
skip-validation), `stages.py` (`STAGE_NAMES` + one adapter per stage +
`STAGES` registry; each LLM-stage adapter owns its `llm_report.Recorder`
lifecycle), and `cli.py` (same flags as the old bash script; CLI values become
validated config overrides via `get_effective_config`, so precedence is
CLI > YAML > defaults). `scripts/run_pipeline.sh` is now a 6-line shim that
execs `uv run python -m nagare_clip.pipeline "$@"`, so user-facing usage is
unchanged. Every stage exposes a typed `run()` in `src/nagare_clip/<stage>/run.py`;
the eight per-stage `cli.py` files (and the `nagare-clip-intervals` console
script) were deleted — `python -m nagare_clip` is re-aliased from the
intervals stage to the pipeline CLI. Single-stage runs now use
`--from-stage X --to-stage X` on the pipeline CLI. Unknown stage names and
inverted `--from-stage`/`--to-stage` ranges raise a friendly `PipelineError`
(the old bash `stage_index` set-e quirk is gone). See
[`docs/stages/pipeline.md`](docs/stages/pipeline.md) for full runtime detail.

## sentence_split Stage

**Status: complete** (branch `sentence-split-stage`, Tasks 1–7; design in `docs/superpowers/specs/2026-06-29-sentence-split-stage-design.md`).

The `sentence_split` stage sits between `audio_silence` and `text_filter` and is disabled by default (byte-identical copy-through when `sentence_split.enabled: false`). When enabled, it rewrites the WhisperX `{stem}.json` + `{stem}.txt` into one-sentence-per-line units using a **bunsetsu-index-range approach**: the LLM receives a numbered list of GiNZA bunsetsu units and returns contiguous index ranges (`{"sentences":[[a,b],…]}`), which the stage maps back to whole-word boundaries via `char2word` and uses to reassemble segments from the original word list. Words are only reassigned, never edited, so word timings are verbatim by construction. A final `concat_word_text` check guards the verbatim invariant. Processing is windowed (`window_segments`, default 20 — the batch size) for long transcripts; each window degrades independently on LLM failure. Windows carry their trailing (possibly incomplete) sentence into the next window so a sentence straddling a window boundary is re-grouped rather than split at the seam; a single-sentence window emits as-is and resets the carry (run-on guard), and on degrade the carried sentence is flushed before falling back to the original segments. All downstream stages read from `output/sentence_split/`.

## Langfuse LLM Tracing

**Status: complete** (branch `feat/langfuse-tracing`, Tasks 1–9 committed; Task 10 docs).

Langfuse tracing was implemented at the single LLM chokepoint — `nagare_clip.llm_client.call_llm` — via LiteLLM's `langfuse_otel` OTEL callback (`litellm.callbacks = ["langfuse_otel"]`). The callback is registered once per process by `_ensure_tracing()` (idempotent) and flushed on exit by an `atexit` hook (`flush_traces()`, a workaround for short-lived CLI processes). Tracing is env-gated: enabled only when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both set AND `NAGARE_LANGFUSE != "0"` AND `general.langfuse` (config, default `true`) is not false; when disabled the provider call is byte-identical to the pre-tracing behaviour. Each call carries `generation_name="<stage>/<unit>"` and `tags=["stage:<stage>","stem:<unit>"]`; `session_id` is set to `NAGARE_RUN_ID` (one timestamp per `run_pipeline.sh` invocation, so all calls in a run group under one Langfuse session). Metadata is carried via `with_trace_meta(cfg, stage=..., unit=...)` under a reserved `cfg["_trace"]` key that `call_llm` pops before forwarding kwargs to the provider. `run_pipeline.sh` exports `NAGARE_RUN_ID` and maps `general.langfuse: false` to `NAGARE_LANGFUSE=0` for all stage subprocesses. The existing markdown `llm_report` is untouched and runs alongside as an independent sink. Documented fallback: if OTEL flushing proves unreliable, switch to the langfuse-SDK callback (`["langfuse"]`) with explicit `langfuse.flush()` in `flush_traces`. This is observability-only and decoupled from any future LangGraph migration.
