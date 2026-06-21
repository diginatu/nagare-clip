# Langfuse Tracing Integration — Design

Date: 2026-06-22
Status: Approved design, pre-implementation

## Goal

Add **Langfuse observability/tracing** to nagare-clip's LLM stages. Capture every
LLM call (prompts, response, model, token usage, latency, errors) as Langfuse
traces, grouped by pipeline run, stage, and source unit. Scope is **tracing
only** — no prompt management, no datasets/evaluation in this change.

The existing markdown `llm_report.py` stays **fully intact and runs alongside**.
Langfuse is an additional, optional sink.

## Non-goals

- Prompt management (pulling prompts from Langfuse registry).
- Datasets / evaluations / scoring.
- Self-hosting setup (works against Langfuse Cloud or any configured host, but we
  do not stand up an instance here).
- Replacing or modifying `llm_report.py` behavior.

## Integration mechanism (docs-verified)

Use LiteLLM's native OpenTelemetry callback: `litellm.callbacks = ["langfuse_otel"]`.
This is the integration documented at
`https://langfuse.com/integrations/frameworks/litellm-sdk`. The callback
auto-captures request/response/usage/latency/errors and exports them over OTLP
HTTP to Langfuse.

`llm_client.call_llm` is the single chokepoint for **every** stage's LLM call
(text_filter + summary_llm, summary, plan, director, guided_edit), so registering
the callback there lights up tracing for all stages with **no stage-logic
changes**.

### Configuration & enablement (off by default)

Env-gated, zero-config-by-default:

- Registers the callback **only** when both `LANGFUSE_PUBLIC_KEY` and
  `LANGFUSE_SECRET_KEY` are present in the environment.
- `LANGFUSE_OTEL_HOST` selects region / self-hosted endpoint (per docs; default
  is US cloud). Passed through purely via env — we do not hardcode a host.
- A `general.langfuse` boolean in `config.py` `DEFAULTS` (default `true`) and
  documented in `config.example.yml` lets a project force-disable even when keys
  are present. Disabled OR keys absent → **no callback registered, no `metadata`
  added** → behavior byte-identical to today (regression-guarded).

Because the callback is global LiteLLM state, registration is **idempotent** and
**lazy**: performed once on the first `call_llm` invocation (guarded by a
module-level flag), so merely importing `llm_client` has no side effects and unit
tests stay clean.

### Dependencies

Add to `pyproject.toml` `dependencies` (litellm already present):

- `opentelemetry-api`
- `opentelemetry-sdk`
- `opentelemetry-exporter-otlp`

## Grouping & labeling

Chosen granularity: **run + stage + unit.**

| Langfuse field    | Source                                                            | Purpose                          |
| ----------------- | ---------------------------------------------------------------- | -------------------------------- |
| `session_id`      | `NAGARE_RUN_ID` env var, set once by `run_pipeline.sh` per run    | Group all calls of one run       |
| `generation_name` | `"<stage>/<unit>"` (e.g. `director/myvideo`)                      | Identify the call in the UI list |
| `tags`            | `["stage:<stage>", "stem:<unit>"]`                               | Filter by stage or source        |

LiteLLM maps a `metadata={...}` kwarg on `completion()` to `langfuse.*` span
attributes (`generation_name`, `session_id`, `tags`, etc.), per the docs'
"Metadata Support" section.

### How metadata reaches `call_llm` (minimal-churn threading)

`call_llm(messages, cfg)` already receives the per-stage `cfg` dict at every call
site, and stages pass `cfg`/`attempt_cfg` copies through injected `call_llm`
fakes in tests. We carry trace metadata **inside `cfg` under a reserved key**, so:

- No signature change to `call_llm` or the `CallLLM` protocol.
- Test doubles that take `(messages, cfg)` keep working — they simply ignore the
  extra key.

A new pure helper builds the reserved entry:

```python
# nagare_clip/llm_client.py (or a small tracing helper)
def with_trace_meta(cfg, *, stage, unit, extra_tags=()):
    out = dict(cfg)
    out["_trace"] = {
        "generation_name": f"{stage}/{unit}",
        "tags": [f"stage:{stage}", f"stem:{unit}", *extra_tags],
    }
    return out
```

Each of the ~6 LLM-calling stage functions wraps its cfg once (before the retry
loop) with `with_trace_meta(...)`; `cfg_for_attempt` copies preserve `_trace`.

`call_llm` then:

1. Pops `_trace` from cfg (so it never leaks into LiteLLM provider kwargs).
2. Reads `NAGARE_RUN_ID` from env for `session_id`.
3. If tracing is enabled, builds `metadata = {**_trace, "session_id": run_id}`
   (omitting `session_id` when the env var is unset) and passes `metadata=` to
   `litellm.completion`.
4. If tracing is disabled, adds **no** `metadata` kwarg (regression-safe).

`run_pipeline.sh` exports `NAGARE_RUN_ID="$(date +%Y%m%d-%H%M%S)"` once near the
top, before any stage runs, so every stage subprocess inherits the same session.
Standalone CLI runs without the env var simply get no `session_id` (still traced).

## Operational risk: span flush in short-lived processes

Each stage runs as a **separate short-lived CLI process**. OTEL exporters batch
spans and may exit before exporting, dropping traces. Mitigation:

- Best-effort **force-flush on exit**: register an `atexit` handler (or a
  `finally` in each stage CLI) that calls the OTEL tracer provider's
  `force_flush()` inside a `try/except`, only when tracing was enabled.
- **Verification gate (implementation):** after wiring, run a real stage against
  a Langfuse project and confirm a trace with the expected session/stage/tags
  actually appears — do not claim done on green unit tests alone.
- **Documented fallback:** if `langfuse_otel` flushing proves unreliable for
  short scripts, switch the callback to the langfuse-SDK integration
  (`litellm.success_callback = ["langfuse"]` + `litellm.failure_callback`), which
  exposes a deterministic `langfuse.flush()`/`shutdown()`. Same metadata model;
  contained to `llm_client.py`. This fallback is recorded here so the choice is
  reversible without re-deciding the architecture.

## Components touched

- `src/nagare_clip/llm_client.py` — lazy idempotent callback registration,
  env/config gating, `metadata` assembly, `_trace` pop, `with_trace_meta` helper,
  atexit force-flush.
- `src/nagare_clip/config.py` — `general.langfuse` default.
- `config.example.yml` — document `general.langfuse` + the `LANGFUSE_*` env vars.
- Each LLM-calling stage function (text_filter `llm_filter`/`summary_llm`,
  summary `summarize`, plan `plan_llm`, director `director_llm`, guided_edit
  `apply`) — one `with_trace_meta(cfg, stage=..., unit=...)` wrap each.
- `scripts/run_pipeline.sh` — export `NAGARE_RUN_ID`.
- `pyproject.toml` — OTEL deps.
- Docs: `README.md`, `plan.md`, `AGENTS.md` per the project's Documentation
  Policy.

## Testing strategy (TDD, with mutation-catch verification)

Unit-testable by monkeypatching `litellm.completion` and `litellm.callbacks`:

1. **Enabled registration:** with both keys in env (monkeypatched) and
   `general.langfuse` true, `call_llm` registers `"langfuse_otel"` in
   `litellm.callbacks` exactly once across multiple calls (idempotent).
2. **Disabled paths:** keys absent → callback NOT registered and **no `metadata`
   kwarg** passed to `completion` (assert exact kwargs); `general.langfuse` false
   with keys present → same.
3. **Metadata assembly:** with `_trace` in cfg and `NAGARE_RUN_ID` set,
   `completion` receives `metadata` with the expected `generation_name`, `tags`,
   and `session_id`; `_trace` is absent from the kwargs forwarded to the provider.
4. **No env session:** `NAGARE_RUN_ID` unset → `metadata` omits `session_id` but
   still carries name/tags.
5. **`with_trace_meta` helper:** pure-function test of the produced dict shape.
6. **Regression:** existing `call_llm` kwargs (model/messages/temperature/
   api_base/response_format/reasoning_effort) are unchanged when tracing is off.

Per repo TDD rules: for each behavior, confirm the test fails against a broken
implementation (e.g. always-register, never-pop `_trace`, drop `session_id`)
before reverting, and report mutation-catch evidence alongside the green run.
Plus the live verification gate above for the flush concern.

## Out-of-scope follow-ups (not in this change)

- LangGraph migration — explicitly decided **not** a prerequisite; Langfuse is
  decoupled from orchestration and carries over if LangGraph is adopted later.
- Prompt management and evals — separate future specs.
