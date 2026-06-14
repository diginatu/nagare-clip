# External LLM Provider Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let every LLM stage target OpenAI, Gemini, Anthropic, or Ollama — configured per stage — by routing all calls through a single LiteLLM-backed client.

**Architecture:** Replace the Ollama-only `urllib` transport with a new package-root module `llm_client.py` exposing `call_llm(messages, cfg) -> str` built on `litellm.completion`. Provider is chosen by a per-stage `provider` config key (LiteLLM prefix, default `ollama_chat`). `stage2/llm_filter._call_llm` becomes a thin alias so the five existing importers (`text_filter`, `summary`, `plan`, `director`, `guided_edit`) need no changes.

**Tech Stack:** Python 3.11+, uv, LiteLLM, pytest.

---

## File Structure

- **Create** `src/nagare_clip/llm_client.py` — the unified LLM transport (one responsibility: turn a `cfg` + `messages` into a text completion via LiteLLM).
- **Create** `tests/test_llm_client.py` — unit tests mocking `litellm.completion`.
- **Modify** `pyproject.toml` — add `litellm` runtime dependency.
- **Modify** `src/nagare_clip/stage2/llm_filter.py` — delete the urllib `_call_llm` body + dead imports; alias `_call_llm` to `llm_client.call_llm`.
- **Modify** `src/nagare_clip/config.py` — add `provider` and change `api_base` default to `""` in the six LLM config blocks.
- **Modify** `config.example.yml`, `README.md`, `plan.md`, `CLAUDE.md` (AGENTS.md) — docs.

---

## Task 1: Add the LiteLLM dependency

**Files:**
- Modify: `pyproject.toml` (dependencies array, lines 9-13)

- [ ] **Step 1: Add the dependency via uv**

Run:
```bash
uv add litellm
```
Expected: `pyproject.toml` `dependencies` now lists `litellm`, and `uv.lock` updates. If the sandbox blocks the uv cache, re-run with the network/cache available.

- [ ] **Step 2: Verify the import resolves**

Run:
```bash
uv run python -c "import litellm; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add litellm dependency"
```

---

## Task 2: Create the LiteLLM-backed client

**Files:**
- Create: `src/nagare_clip/llm_client.py`
- Test: `tests/test_llm_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_llm_client.py`:

```python
"""Unit tests for the LiteLLM-backed unified LLM client."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nagare_clip import llm_client


def _fake_response(content: str):
    """Mimic the litellm.completion return shape we read from."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _call(cfg):
    with patch("nagare_clip.llm_client.litellm.completion") as m:
        m.return_value = _fake_response("RESULT")
        out = llm_client.call_llm([{"role": "user", "content": "hi"}], cfg)
    return out, m


def test_returns_message_content():
    out, _ = _call({"provider": "openai", "model": "gpt-4o"})
    assert out == "RESULT"


def test_provider_model_composition():
    _, m = _call({"provider": "openai", "model": "gpt-4o"})
    assert m.call_args.kwargs["model"] == "openai/gpt-4o"


def test_default_provider_is_ollama_chat():
    _, m = _call({"model": "qwen3.5:4b"})
    assert m.call_args.kwargs["model"] == "ollama_chat/qwen3.5:4b"


def test_empty_ollama_api_base_falls_back_to_localhost():
    _, m = _call({"provider": "ollama_chat", "model": "x", "api_base": ""})
    assert m.call_args.kwargs["api_base"] == "http://localhost:11434"


def test_explicit_api_base_is_forwarded():
    _, m = _call({"provider": "ollama_chat", "model": "x",
                  "api_base": "http://host:9999"})
    assert m.call_args.kwargs["api_base"] == "http://host:9999"


def test_cloud_provider_with_empty_api_base_passes_none():
    _, m = _call({"provider": "openai", "model": "gpt-4o", "api_base": ""})
    assert "api_base" not in m.call_args.kwargs


def test_response_format_json_translated():
    _, m = _call({"provider": "openai", "model": "x", "response_format": "json"})
    assert m.call_args.kwargs["response_format"] == {"type": "json_object"}


def test_no_response_format_key_when_unset():
    _, m = _call({"provider": "openai", "model": "x"})
    assert "response_format" not in m.call_args.kwargs


def test_thinking_level_maps_to_reasoning_effort():
    _, m = _call({"provider": "openai", "model": "x", "thinking": "high"})
    assert m.call_args.kwargs["reasoning_effort"] == "high"


def test_thinking_true_maps_to_low():
    _, m = _call({"provider": "openai", "model": "x", "thinking": True})
    assert m.call_args.kwargs["reasoning_effort"] == "low"


def test_thinking_false_omits_reasoning_effort():
    _, m = _call({"provider": "openai", "model": "x", "thinking": False})
    assert "reasoning_effort" not in m.call_args.kwargs


def test_api_key_forwarded_when_set():
    _, m = _call({"provider": "openai", "model": "x", "api_key": "sk-123"})
    assert m.call_args.kwargs["api_key"] == "sk-123"


def test_litellm_error_wrapped_as_connection_error():
    with patch("nagare_clip.llm_client.litellm.completion", side_effect=ValueError("boom")):
        with pytest.raises(ConnectionError):
            llm_client.call_llm([{"role": "user", "content": "hi"}],
                                {"provider": "openai", "model": "x"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/test_llm_client.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.llm_client'` (or `AttributeError` on `call_llm`).

- [ ] **Step 3: Write the implementation**

Create `src/nagare_clip/llm_client.py`:

```python
"""Unified LLM client backed by LiteLLM.

Routes every stage's chat call through ``litellm.completion`` so a stage can
target OpenAI, Gemini, Anthropic, or a local Ollama purely from config. The
provider is chosen by ``cfg['provider']`` (a LiteLLM prefix, default
``"ollama_chat"``); the model id handed to LiteLLM is ``f"{provider}/{model}"``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import litellm

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_API_BASE = "http://localhost:11434"


def call_llm(messages: List[Dict[str, str]], cfg: Dict[str, Any]) -> str:
    """Send chat ``messages`` to the configured provider, return text content.

    Falls back to the local Ollama base URL when an Ollama provider is selected
    with an empty ``api_base``. Any LiteLLM error is re-raised as
    ``ConnectionError`` so the stages' existing broad-``Exception`` retry loops
    treat it like the old urllib transport did.
    """
    provider = cfg.get("provider", "ollama_chat")
    model = cfg.get("model", "")

    kwargs: Dict[str, Any] = {
        "model": f"{provider}/{model}",
        "messages": messages,
        "temperature": cfg.get("temperature", 0.1),
        "timeout": cfg.get("timeout", 300),
    }

    api_base = cfg.get("api_base", "")
    if not api_base and provider.startswith("ollama"):
        api_base = DEFAULT_OLLAMA_API_BASE
    if api_base:
        kwargs["api_base"] = api_base

    api_key = cfg.get("api_key", "")
    if api_key:
        kwargs["api_key"] = api_key

    if cfg.get("response_format") == "json":
        kwargs["response_format"] = {"type": "json_object"}

    thinking = cfg.get("thinking", False)
    if thinking:
        kwargs["reasoning_effort"] = thinking if isinstance(thinking, str) else "low"

    logger.debug("LLM request model=%s", kwargs["model"])
    try:
        response = litellm.completion(**kwargs)
    except Exception as e:  # noqa: BLE001 - normalize for the stages' retry loops
        raise ConnectionError(f"LLM API request failed: {e}") from e

    return response.choices[0].message.content
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/test_llm_client.py -v
```
Expected: PASS (all 12 tests).

- [ ] **Step 5: Verify the tests catch a broken implementation (mutation check)**

Per repo TDD policy, confirm each guard actually fails on a mutation. Temporarily edit `src/nagare_clip/llm_client.py`:
- Change `f"{provider}/{model}"` to `f"{model}"` → run `uv run pytest tests/test_llm_client.py -v`, confirm `test_provider_model_composition` / `test_default_provider_is_ollama_chat` FAIL, then revert.
- Change the `except` to `raise` a plain `RuntimeError` (or remove the wrap) → confirm `test_litellm_error_wrapped_as_connection_error` FAILS, then revert.
- Change `{"type": "json_object"}` to `{"type": "text"}` → confirm `test_response_format_json_translated` FAILS, then revert.

Re-run `uv run pytest tests/test_llm_client.py -v` and confirm all PASS after reverting.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/llm_client.py tests/test_llm_client.py
git commit -m "feat: add LiteLLM-backed unified llm_client"
```

---

## Task 3: Route `_call_llm` through the new client

**Files:**
- Modify: `src/nagare_clip/stage2/llm_filter.py` (imports lines 5-9; `_call_llm` function lines 184-222)

- [ ] **Step 1: Replace the `_call_llm` function with an alias**

In `src/nagare_clip/stage2/llm_filter.py`, delete the entire `_call_llm` function (the `def _call_llm(...)` block at lines ~184-222, ending with `return result["message"]["content"]`). Do not delete `filter_transcript`'s `if call_llm is None: call_llm = _call_llm` lines — they still reference the name.

- [ ] **Step 2: Add the alias import and remove dead imports**

At the top of `src/nagare_clip/stage2/llm_filter.py`, remove these now-unused imports:

```python
import json
import urllib.error
import urllib.request
```

Add this import alongside the existing `from nagare_clip.llm_report import ...` line:

```python
from nagare_clip.llm_client import call_llm as _call_llm
```

(`_call_llm` is now imported, so the four internal `call_llm = _call_llm` defaults and the four other stages that `from nagare_clip.stage2.llm_filter import _call_llm` keep working unchanged.)

- [ ] **Step 3: Verify nothing else in the file used the removed imports**

Run:
```bash
grep -nE '\bjson\b|urllib' src/nagare_clip/stage2/llm_filter.py
```
Expected: no matches (all `json`/`urllib` usage lived inside the deleted `_call_llm`).

- [ ] **Step 4: Run the full suite to confirm no regressions**

Run:
```bash
uv run pytest -q
```
Expected: PASS. Existing stage tests inject their own fake `call_llm`, so they never hit LiteLLM; the import alias just needs to resolve.

- [ ] **Step 5: Verify the alias is the real client (mutation check)**

Run:
```bash
uv run python -c "from nagare_clip.stage2.llm_filter import _call_llm; from nagare_clip.llm_client import call_llm; assert _call_llm is call_llm; print('alias ok')"
```
Expected: prints `alias ok`.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/stage2/llm_filter.py
git commit -m "refactor: route _call_llm through llm_client (delete urllib transport)"
```

---

## Task 4: Add `provider` config and default `api_base` to empty

**Files:**
- Modify: `src/nagare_clip/config.py` (six blocks: `api_base` at lines 27, 60, 85, 127, 164, 210)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_llm_sections_default_provider_and_empty_api_base():
    from nagare_clip.config import DEFAULTS

    sections = [
        DEFAULTS["text_filter"],
        DEFAULTS["text_filter"]["summary_llm"],
        DEFAULTS["summary"],
        DEFAULTS["plan"],
        DEFAULTS["director"],
        DEFAULTS["guided_edit"],
    ]
    for sec in sections:
        assert sec["provider"] == "ollama_chat"
        assert sec["api_base"] == ""
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/test_config.py::test_llm_sections_default_provider_and_empty_api_base -v
```
Expected: FAIL — `KeyError: 'provider'`.

- [ ] **Step 3: Edit the six config blocks**

In `src/nagare_clip/config.py`, for each of the six LLM blocks, change the
`api_base` line to `""` and add a `provider` line immediately above it. Apply to:
`text_filter` (line ~27), `text_filter.summary_llm` (line ~60), `summary`
(line ~85), `plan` (line ~127), `director` (line ~164), `guided_edit` (line ~210).

Each block's transport keys become:

```python
        "provider": "ollama_chat",
        "api_base": "",
        "model": "qwen3.5:4b",          # keep each block's existing model value
```

(Keep every block's existing `model`, `temperature`, `timeout`, etc. — only add
`provider` and blank `api_base`. The `summary_llm` block is nested one level
deeper, so match its indentation.)

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/test_config.py -v
```
Expected: PASS.

- [ ] **Step 5: Verify the guard catches a miss (mutation check)**

Temporarily revert just the `director` block's `api_base` back to
`"http://localhost:11434"`, run
`uv run pytest tests/test_config.py::test_llm_sections_default_provider_and_empty_api_base -v`,
confirm it FAILS, then restore `""`.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/config.py tests/test_config.py
git commit -m "feat: add per-stage provider config, default api_base empty"
```

---

## Task 5: Documentation

**Files:**
- Modify: `config.example.yml`
- Modify: `README.md`
- Modify: `plan.md`
- Modify: `CLAUDE.md` (AGENTS.md)

- [ ] **Step 1: Update `config.example.yml`**

For each LLM section (`text_filter`, its nested `summary_llm`, `summary`,
`plan`, `director`, `guided_edit`), add a `provider:` line above `api_base:` and
update the `api_base:` comment. Example for `text_filter` (mirror the shape in
the other sections):

```yaml
  provider: "ollama_chat"      # LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic
  api_base: ""                 # Base URL; empty -> Ollama localhost default; leave empty for cloud providers
  model: "qwen3.5:4b"          # Model name (passed to LiteLLM as "<provider>/<model>")
  api_key: ""                  # API key for the provider (or set the provider's env var, e.g. OPENAI_API_KEY)
```

Add a short comment block near the top of the file documenting that cloud
providers (`openai`, `gemini`, `anthropic`) read their key from `api_key` or the
provider's standard environment variable, and use the provider's default
endpoint when `api_base` is empty.

- [ ] **Step 2: Update `README.md`**

Add a subsection (under the configuration/usage area) titled "Choosing an LLM
provider" explaining: set `provider:` to `ollama_chat` (default), `openai`,
`gemini`, or `anthropic` on any LLM stage; set `model:` to that provider's model
name; provide `api_key:` (or the provider env var); leave `api_base:` empty for
cloud providers. Mention each stage can use a different provider.

- [ ] **Step 3: Update `plan.md`**

Add a status entry: external LLM providers supported via LiteLLM; all stages
route through `nagare_clip.llm_client.call_llm`; per-stage `provider` config key
(default `ollama_chat`).

- [ ] **Step 4: Update `CLAUDE.md` (AGENTS.md)**

- In **Project Structure**, add `llm_client.py` to the package-root file list:
  `llm_client.py  # Unified LiteLLM-backed transport: call_llm(messages, cfg) -> str`.
- In **Hard Constraints**, note LiteLLM is the LLM transport dependency.
- In **Configuration System** / **Current Runtime Quirks**, replace the wording
  that LLM stages call "Ollama native chat API" with: all LLM stages route
  through `llm_client.call_llm` (LiteLLM); provider is selected per stage by the
  `provider` key (default `ollama_chat`), model passed as `<provider>/<model>`,
  `api_base` empty falls back to Ollama localhost / is omitted for cloud
  providers, `response_format: "json"` maps to a JSON-object request, and
  `thinking` maps to LiteLLM `reasoning_effort` (best-effort per provider).

- [ ] **Step 5: Verify docs reference real symbols**

Run:
```bash
grep -rn "llm_client" README.md plan.md CLAUDE.md config.example.yml
```
Expected: matches in the edited files, all referring to `llm_client.call_llm` / the module.

- [ ] **Step 6: Commit**

```bash
git add config.example.yml README.md plan.md CLAUDE.md
git commit -m "docs: document external LLM providers (LiteLLM, per-stage provider)"
```

---

## Final verification

- [ ] Run the whole suite once more:

```bash
uv run pytest -q
```
Expected: PASS (including the new `tests/test_llm_client.py` and the config test).

- [ ] Confirm the Ollama default path is unchanged behaviorally: a stage cfg
  with no `provider`/`api_base` resolves to `ollama_chat/<model>` against
  `http://localhost:11434` (covered by `test_default_provider_is_ollama_chat`
  and `test_empty_ollama_api_base_falls_back_to_localhost`).
