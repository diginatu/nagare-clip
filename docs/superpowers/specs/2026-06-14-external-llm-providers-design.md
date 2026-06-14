# External LLM Provider Support via LiteLLM

**Date:** 2026-06-14
**Status:** Approved design

## Goal

Let every LLM stage target external cloud providers — OpenAI, Gemini, and
Anthropic — in addition to the existing local Ollama, configured per stage.
Today all LLM calls hard-code Ollama's native `/api/chat` wire format
(`stage2/llm_filter._call_llm`). Anthropic in particular does not speak the
OpenAI `/chat/completions` shape, so a hand-rolled OpenAI-compatible adapter
would not cover all four. We adopt **LiteLLM** — one pure-Python dependency that
normalizes OpenAI / Gemini / Anthropic / Ollama behind a single
`completion()` call.

## Current architecture (the leverage point)

Every LLM stage funnels through one function:

```python
_call_llm(messages: list[dict], cfg: dict) -> str   # in stage2/llm_filter.py
```

The five callers — `text_filter` (incl. its nested `summary_llm`), `summary`,
`plan`, `director`, `guided_edit` — import it and inject it as the default
`call_llm` parameter, so stage logic and tests are already decoupled from the
transport. Changing the transport is therefore a single-function change.

The current body POSTs to `{api_base}/api/chat` via stdlib `urllib`, reads
`result["message"]["content"]`, and uses Ollama-only fields (`think`,
`format: "json"`). Both retry loops (`director`, `guided_edit` via
`llm_retry`) and `text_filter`'s batch-halving loop catch broad `Exception`, so
any transport error already triggers the existing retry / temperature-nudge
behavior.

## Design

### 1. New module: `src/nagare_clip/llm_client.py`

Sibling to the existing `llm_retry.py` / `llm_report.py` package-root helpers.
Exposes the existing contract:

```python
def call_llm(messages: list[dict], cfg: dict) -> str
```

Behavior:

- Compose the LiteLLM model id as `f"{provider}/{model}"`.
- Call `litellm.completion(model=..., messages=messages, ...)`.
- Return `response.choices[0].message.content`.
- Wrap any LiteLLM exception in `ConnectionError` (keeps logs clean; retries
  already fire on broad `Exception`, so this is for clarity, not load-bearing).

`stage2/llm_filter.py` keeps the `_call_llm` name as a thin alias:

```python
from nagare_clip.llm_client import call_llm as _call_llm
```

so all five existing importers keep working untouched. The old urllib body is
deleted. (Optional follow-up: migrate the other modules to import from
`llm_client` directly — low risk, more honest, not required.)

### 2. Config: a per-stage `provider` field

LiteLLM selects the provider via the `provider/model` prefix. Each LLM section
already carries `model`, `api_base`, `api_key`, `temperature`; add one key:

```yaml
provider: ollama_chat   # | openai | gemini | anthropic
```

The same key applies to the nested `text_filter.summary_llm` block (it passes
its own cfg dict, so no special-casing).

**Backward compatibility — simplest implementation:**

- `provider` defaults to `ollama_chat`. Existing configs
  (`model: qwen3.5:4b`, `api_base: http://localhost:11434`) keep working with no
  edits.
- `api_base` is forwarded to LiteLLM **only when non-empty**; the Ollama path
  falls back to `http://localhost:11434` when empty. The per-stage default
  `api_base` changes from `http://localhost:11434` to `""`. This is the one
  behavioral default change — Ollama still resolves correctly via the fallback,
  and cloud providers are not handed a stray localhost base. No extra
  machinery beyond this.

### 3. Field translation (LiteLLM normalizes per-provider quirks)

| cfg key | LiteLLM mapping |
|---|---|
| `response_format: "json"` | `response_format={"type": "json_object"}` |
| `thinking: true / "low" / "medium" / "high"` | `reasoning_effort=...` (best-effort, capable models) |
| `temperature`, `timeout`, `api_key` | passed through |

JSON-mode and thinking are best-effort per provider; the stage parse helpers
already degrade gracefully, so an unsupported combination never crashes a stage.

### 4. Dependency

Add `litellm` to `pyproject.toml` via `uv add litellm`.

## Testing (TDD)

Stage functions already inject `call_llm`, so existing stage tests stay offline
and unchanged. New `tests/test_llm_client.py` mocks `litellm.completion` and
asserts:

- `provider/model` composition (e.g. `ollama_chat/qwen3.5:4b`, `openai/gpt-4o`).
- `api_base` forwarded only when set; empty Ollama base falls back to localhost;
  cloud provider with empty base passes no `api_base`.
- `response_format: "json"` → `{"type": "json_object"}`.
- `thinking` levels → `reasoning_effort`.
- Response extraction from `choices[0].message.content`.
- LiteLLM exception → `ConnectionError`.

Each test is verified to fail against a deliberately mutated client before the
real implementation lands (red-green discipline).

## Documentation

Update when behavior lands:

- `config.example.yml` — document `provider` on every LLM section + the new
  `api_base: ""` default.
- `README.md` — user-facing: how to point a stage at OpenAI / Gemini /
  Anthropic.
- `plan.md` — implementation/status.
- `AGENTS.md` — the "all LLM stages route through `_call_llm`" note becomes
  "route through `llm_client.call_llm`"; document the `provider` config and the
  LiteLLM dependency in Hard Constraints / Configuration sections.

## Out of scope

- Streaming responses (all stages are non-streaming today).
- Per-provider safety-setting / advanced-param passthrough beyond the table
  above.
- Removing the `_call_llm` alias or migrating import paths (optional follow-up).
