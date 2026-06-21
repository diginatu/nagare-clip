# Langfuse Tracing Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trace every LLM call across all nagare-clip stages into Langfuse, grouped by pipeline run / stage / source, gated off by default — without changing stage logic or removing the existing markdown LLM report.

**Architecture:** Register LiteLLM's native `langfuse_otel` callback once at the single LLM chokepoint (`llm_client.call_llm`). Carry per-call grouping metadata (`session_id`, `generation_name`, `tags`) inside the existing `cfg` dict under a reserved `_trace` key, which `call_llm` converts to a LiteLLM `metadata=` kwarg. Enablement is env-gated; `run_pipeline.sh` provides the per-run session id and config kill-switch via env vars.

**Tech Stack:** Python, LiteLLM (already a dep), OpenTelemetry (new deps), Langfuse Cloud/self-hosted, pytest, bash.

## Global Constraints

- Dependency management uses uv + pyproject.toml. (run `uv sync` / `uv run` — never pip)
- LiteLLM is the only LLM transport: all provider access goes through `nagare_clip.llm_client.call_llm`. Do not add provider-specific HTTP clients.
- Integration mechanism is the documented `litellm.callbacks = ["langfuse_otel"]` (per https://langfuse.com/integrations/frameworks/litellm-sdk). Do NOT invent a custom exporter.
- Tracing is OFF by default: enabled iff `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set AND `os.environ.get("NAGARE_LANGFUSE", "1") != "0"`.
- When tracing is disabled, `call_llm` must pass **no** `metadata` kwarg and must **not** mutate `litellm.callbacks` — byte-identical to today's behavior.
- TDD: write the failing test first, see it fail, then implement. For each behavior, also confirm the test catches a broken implementation (mutation) and report that evidence.
- Keep `llm_report.py` untouched in behavior; Langfuse is an additional sink.
- Run validation with `uv run pytest` and `uv run python -m py_compile`.

---

### Task 1: Add OpenTelemetry dependencies

**Files:**
- Modify: `pyproject.toml` (the `dependencies = [...]` array)

**Interfaces:**
- Produces: importable `opentelemetry` packages for later tasks.

- [ ] **Step 1: Add the three OTEL packages**

In `pyproject.toml`, change the `dependencies` array to:

```toml
dependencies = [
    "PyYAML",
    "ginza",
    "ja_ginza",
    "litellm>=1.89.0",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp",
]
```

- [ ] **Step 2: Sync and verify the packages import**

Run:
```bash
uv sync
uv run python -c "import opentelemetry.trace, opentelemetry.sdk, opentelemetry.exporter.otlp; print('otel ok')"
```
Expected: `otel ok` (and `uv.lock` updated).

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add OpenTelemetry deps for Langfuse tracing"
```

---

### Task 2: `with_trace_meta` helper

**Files:**
- Modify: `src/nagare_clip/llm_client.py`
- Test: `tests/test_llm_client_tracing.py` (create)

**Interfaces:**
- Produces: `with_trace_meta(cfg: Dict[str, Any], *, stage: str, unit: str, extra_tags: Iterable[str] = ()) -> Dict[str, Any]` — returns a shallow copy of `cfg` with a reserved `"_trace"` dict: `{"generation_name": "<stage>/<unit>" (or just "<unit>" if stage is falsy), "tags": ["stage:<stage>", "stem:<unit>", *extra_tags]}`. Never mutates `cfg`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_llm_client_tracing.py`:

```python
from nagare_clip import llm_client


def test_with_trace_meta_builds_reserved_entry():
    cfg = {"model": "m", "temperature": 0.1}
    out = llm_client.with_trace_meta(cfg, stage="director", unit="myvideo")
    assert out["_trace"] == {
        "generation_name": "director/myvideo",
        "tags": ["stage:director", "stem:myvideo"],
    }
    # original cfg is untouched and other keys preserved
    assert "_trace" not in cfg
    assert out["model"] == "m" and out["temperature"] == 0.1


def test_with_trace_meta_extra_tags_and_empty_stage():
    out = llm_client.with_trace_meta({}, stage="", unit="overall", extra_tags=["x"])
    assert out["_trace"]["generation_name"] == "overall"
    assert out["_trace"]["tags"] == ["stage:", "stem:overall", "x"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_client_tracing.py -v`
Expected: FAIL — `AttributeError: module 'nagare_clip.llm_client' has no attribute 'with_trace_meta'`.

- [ ] **Step 3: Implement the helper**

In `src/nagare_clip/llm_client.py`, update the imports line `from typing import Any, Dict, List` to `from typing import Any, Dict, Iterable, List` and add after the `DEFAULT_OLLAMA_API_BASE` constant:

```python
def with_trace_meta(
    cfg: Dict[str, Any],
    *,
    stage: str,
    unit: str,
    extra_tags: Iterable[str] = (),
) -> Dict[str, Any]:
    """Return a copy of *cfg* carrying Langfuse grouping metadata under ``_trace``.

    ``call_llm`` pops ``_trace`` and forwards it (plus the run session id) to
    LiteLLM as ``metadata``.  Pure: *cfg* is never mutated.
    """
    out = dict(cfg)
    out["_trace"] = {
        "generation_name": f"{stage}/{unit}" if stage else str(unit),
        "tags": [f"stage:{stage}", f"stem:{unit}", *extra_tags],
    }
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_client_tracing.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-catch check**

Temporarily change `f"{stage}/{unit}"` to `f"{stage}_{unit}"`, run the test, confirm `test_with_trace_meta_builds_reserved_entry` FAILS, then revert and re-run to PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/llm_client.py tests/test_llm_client_tracing.py
git commit -m "feat(tracing): add with_trace_meta helper for Langfuse grouping"
```

---

### Task 3: Tracing-enabled gate

**Files:**
- Modify: `src/nagare_clip/llm_client.py`
- Test: `tests/test_llm_client_tracing.py`

**Interfaces:**
- Produces: `_tracing_enabled() -> bool` — True iff `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are non-empty env vars AND `os.environ.get("NAGARE_LANGFUSE", "1") != "0"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_llm_client_tracing.py`:

```python
import pytest


@pytest.fixture
def lf_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.delenv("NAGARE_LANGFUSE", raising=False)


def test_tracing_enabled_when_keys_present(lf_keys):
    assert llm_client._tracing_enabled() is True


def test_tracing_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert llm_client._tracing_enabled() is False


def test_tracing_disabled_by_flag(lf_keys, monkeypatch):
    monkeypatch.setenv("NAGARE_LANGFUSE", "0")
    assert llm_client._tracing_enabled() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_client_tracing.py -k tracing_enabled or tracing_disabled -v`
Expected: FAIL — `AttributeError: ... '_tracing_enabled'`.

- [ ] **Step 3: Implement the gate**

In `src/nagare_clip/llm_client.py`, add `import os` near the top imports (after `import logging`), then add:

```python
def _tracing_enabled() -> bool:
    """Whether Langfuse tracing should be active for this process."""
    if os.environ.get("NAGARE_LANGFUSE", "1") == "0":
        return False
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY")) and bool(
        os.environ.get("LANGFUSE_SECRET_KEY")
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_client_tracing.py -v`
Expected: PASS (all tests so far).

- [ ] **Step 5: Mutation-catch check**

Temporarily make `_tracing_enabled` `return True` unconditionally; confirm `test_tracing_disabled_without_keys` FAILS; revert; re-run PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/llm_client.py tests/test_llm_client_tracing.py
git commit -m "feat(tracing): env-gated _tracing_enabled check"
```

---

### Task 4: Flush helper + idempotent callback registration

**Files:**
- Modify: `src/nagare_clip/llm_client.py`
- Test: `tests/test_llm_client_tracing.py`

**Interfaces:**
- Produces:
  - `flush_traces() -> None` — best-effort `force_flush()` on the global OTEL tracer provider; never raises.
  - `_ensure_tracing() -> bool` — when tracing is enabled, appends `"langfuse_otel"` to `litellm.callbacks` exactly once (idempotent across calls and process) and registers `flush_traces` via `atexit`; returns the enabled state.
  - Module global `_TRACING_INITIALIZED: bool` (reset to `False` in tests).
  - Module constant `LANGFUSE_OTEL_CALLBACK = "langfuse_otel"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_llm_client_tracing.py`:

```python
import litellm


@pytest.fixture
def reset_tracing(monkeypatch):
    monkeypatch.setattr(llm_client, "_TRACING_INITIALIZED", False)
    monkeypatch.setattr(litellm, "callbacks", [])
    yield


def test_ensure_tracing_registers_callback_once(lf_keys, reset_tracing, monkeypatch):
    registered = []
    monkeypatch.setattr(llm_client.atexit, "register", lambda fn: registered.append(fn))
    assert llm_client._ensure_tracing() is True
    assert llm_client._ensure_tracing() is True  # second call is a no-op
    assert litellm.callbacks.count("langfuse_otel") == 1
    assert llm_client.flush_traces in registered  # exactly one atexit hook
    assert len(registered) == 1


def test_ensure_tracing_noop_when_disabled(reset_tracing, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert llm_client._ensure_tracing() is False
    assert litellm.callbacks == []


def test_flush_traces_calls_force_flush(monkeypatch):
    calls = []

    class FakeProvider:
        def force_flush(self):
            calls.append(True)

    import opentelemetry.trace as ot
    monkeypatch.setattr(ot, "get_tracer_provider", lambda: FakeProvider())
    llm_client.flush_traces()
    assert calls == [True]


def test_flush_traces_swallows_missing_force_flush(monkeypatch):
    import opentelemetry.trace as ot
    monkeypatch.setattr(ot, "get_tracer_provider", lambda: object())
    llm_client.flush_traces()  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_client_tracing.py -k ensure_tracing or flush -v`
Expected: FAIL — `AttributeError: ... 'atexit'` / `'_ensure_tracing'` / `'flush_traces'`.

- [ ] **Step 3: Implement flush + registration**

In `src/nagare_clip/llm_client.py`, add `import atexit` near the top imports, add the constant and globals after `DEFAULT_OLLAMA_API_BASE`:

```python
LANGFUSE_OTEL_CALLBACK = "langfuse_otel"
_TRACING_INITIALIZED = False
```

Then add:

```python
def flush_traces() -> None:
    """Best-effort flush of pending OTEL spans (short-lived CLI processes)."""
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        force_flush = getattr(provider, "force_flush", None)
        if callable(force_flush):
            force_flush()
    except Exception:  # noqa: BLE001 - flushing must never break the pipeline
        logger.debug("flush_traces failed", exc_info=True)


def _ensure_tracing() -> bool:
    """Register the Langfuse OTEL callback once. Returns the enabled state."""
    global _TRACING_INITIALIZED
    if not _tracing_enabled():
        return False
    if not _TRACING_INITIALIZED:
        callbacks = list(getattr(litellm, "callbacks", []) or [])
        if LANGFUSE_OTEL_CALLBACK not in callbacks:
            callbacks.append(LANGFUSE_OTEL_CALLBACK)
            litellm.callbacks = callbacks
        atexit.register(flush_traces)
        _TRACING_INITIALIZED = True
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_client_tracing.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-catch check**

Temporarily remove the `if LANGFUSE_OTEL_CALLBACK not in callbacks:` guard (always append); confirm `test_ensure_tracing_registers_callback_once` FAILS (count becomes 2 after a real re-append OR the `_TRACING_INITIALIZED` guard still protects it — to truly exercise it, also temporarily set `_TRACING_INITIALIZED` handling aside by appending before the init guard). Simplest reliable mutation: make `_ensure_tracing` skip the `_TRACING_INITIALIZED` set (`pass` instead of `= True`); confirm the atexit `len(registered) == 1` assertion FAILS. Revert; re-run PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/llm_client.py tests/test_llm_client_tracing.py
git commit -m "feat(tracing): idempotent langfuse_otel registration + atexit flush"
```

---

### Task 5: Wire metadata into `call_llm`

**Files:**
- Modify: `src/nagare_clip/llm_client.py` (the `call_llm` function)
- Test: `tests/test_llm_client_tracing.py`

**Interfaces:**
- Consumes: `with_trace_meta` (`_trace` cfg key), `_ensure_tracing`, env `NAGARE_RUN_ID`.
- Produces: `call_llm` behavior — pops `_trace` from cfg always; when tracing enabled, passes `metadata={**_trace, "session_id": NAGARE_RUN_ID?}` to `litellm.completion`; when disabled, passes no `metadata` and leaves `litellm.callbacks` untouched.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_llm_client_tracing.py`:

```python
class _FakeMsg:
    content = "ok"


class _FakeChoice:
    message = _FakeMsg()


class _FakeResp:
    choices = [_FakeChoice()]


def _capture_completion(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _FakeResp()

    monkeypatch.setattr(litellm, "completion", fake_completion)
    return captured


def test_call_llm_passes_metadata_when_enabled(lf_keys, reset_tracing, monkeypatch):
    monkeypatch.setattr(llm_client.atexit, "register", lambda fn: None)
    monkeypatch.setenv("NAGARE_RUN_ID", "run-123")
    captured = _capture_completion(monkeypatch)

    cfg = llm_client.with_trace_meta(
        {"provider": "openai", "model": "gpt-4o-mini"}, stage="director", unit="vid"
    )
    out = llm_client.call_llm([{"role": "user", "content": "hi"}], cfg)

    assert out == "ok"
    assert captured["metadata"] == {
        "generation_name": "director/vid",
        "tags": ["stage:director", "stem:vid"],
        "session_id": "run-123",
    }
    assert "_trace" not in captured  # never leaks to the provider call


def test_call_llm_omits_session_when_no_run_id(lf_keys, reset_tracing, monkeypatch):
    monkeypatch.setattr(llm_client.atexit, "register", lambda fn: None)
    monkeypatch.delenv("NAGARE_RUN_ID", raising=False)
    captured = _capture_completion(monkeypatch)

    cfg = llm_client.with_trace_meta({"provider": "openai", "model": "m"}, stage="plan", unit="plan")
    llm_client.call_llm([{"role": "user", "content": "hi"}], cfg)

    assert "session_id" not in captured["metadata"]
    assert captured["metadata"]["generation_name"] == "plan/plan"


def test_call_llm_no_metadata_when_disabled(reset_tracing, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    captured = _capture_completion(monkeypatch)

    cfg = llm_client.with_trace_meta({"provider": "openai", "model": "m"}, stage="plan", unit="p")
    llm_client.call_llm([{"role": "user", "content": "hi"}], cfg)

    assert "metadata" not in captured       # regression: identical to pre-tracing
    assert "_trace" not in captured
    assert litellm.callbacks == []          # callbacks untouched when disabled
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_client_tracing.py -k call_llm -v`
Expected: FAIL — `test_call_llm_passes_metadata_when_enabled` errors because `_trace` leaks into `litellm.completion(**kwargs)` (unexpected kwarg) and `metadata` is absent.

- [ ] **Step 3: Implement the wiring**

In `src/nagare_clip/llm_client.py`, replace the body of `call_llm` so it copies cfg, pops `_trace`, and adds metadata. The updated function:

```python
def call_llm(messages: List[Dict[str, str]], cfg: Dict[str, Any]) -> str:
    """Send chat ``messages`` to the configured provider, return text content.

    Falls back to the local Ollama base URL when an Ollama provider is selected
    with an empty ``api_base``. Any LiteLLM error is re-raised as
    ``ConnectionError`` so the stages' existing broad-``Exception`` retry loops
    treat it like the old urllib transport did.
    """
    cfg = dict(cfg)
    trace = cfg.pop("_trace", None)
    provider = cfg.get("provider", "ollama_chat")
    model = cfg.get("model", "")

    kwargs: Dict[str, Any] = {
        "model": f"{provider}/{model}",
        "messages": messages,
        "timeout": cfg.get("timeout", 300),
    }

    temperature = cfg.get("temperature")
    if temperature is not None:
        kwargs["temperature"] = temperature

    api_base = cfg.get("api_base", "").rstrip("/")
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

    if _ensure_tracing():
        metadata = dict(trace) if trace else {}
        run_id = os.environ.get("NAGARE_RUN_ID")
        if run_id:
            metadata["session_id"] = run_id
        if metadata:
            kwargs["metadata"] = metadata

    logger.debug("LLM request model=%s", kwargs["model"])
    try:
        response = litellm.completion(**kwargs)
    except Exception as e:  # noqa: BLE001 - normalize for the stages' retry loops
        raise ConnectionError(f"LLM API request failed: {e}") from e

    return response.choices[0].message.content
```

Note: the existing comment block above `temperature` may be kept; it is omitted here only for brevity — do not delete the original explanatory comments when editing.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm_client_tracing.py -v`
Expected: PASS (all).

- [ ] **Step 5: Mutation-catch check**

(a) Remove `trace = cfg.pop("_trace", None)` and instead `trace = cfg.get("_trace")` (leaving `_trace` in cfg) — confirm `test_call_llm_passes_metadata_when_enabled` FAILS (`_trace` leaks into `completion` kwargs). Revert.
(b) Change `if metadata:` block to always omit metadata — confirm the enabled test FAILS. Revert; re-run PASS.

- [ ] **Step 6: Run the full existing suite (regression)**

Run: `uv run pytest -q`
Expected: PASS — no existing test broken (disabled path is byte-identical).

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/llm_client.py tests/test_llm_client_tracing.py
git commit -m "feat(tracing): pass Langfuse metadata through call_llm"
```

---

### Task 6: `general.langfuse` config default + docs

**Files:**
- Modify: `src/nagare_clip/config.py` (the `general` dict in `DEFAULTS`)
- Modify: `config.example.yml` (the `general:` block)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `get_effective_config(...)["general"]["langfuse"]` defaults to `True`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py` (use the module's existing import of `get_effective_config`; if absent, add `from nagare_clip.config import get_effective_config`):

```python
def test_general_langfuse_defaults_true():
    cfg = get_effective_config(None, {})
    assert cfg["general"]["langfuse"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -k langfuse -v`
Expected: FAIL — `KeyError: 'langfuse'`.

- [ ] **Step 3: Implement the default**

In `src/nagare_clip/config.py`, update the `general` dict:

```python
    "general": {
        "log_level": "INFO",
        "log_file": "",
        "llm_report": True,
        "llm_report_dir": "output/llm_report",
        "langfuse": True,
    },
```

- [ ] **Step 4: Document in `config.example.yml`**

In the `general:` block, after the `llm_report_dir` line, add:

```yaml
  langfuse: true              # send LLM traces to Langfuse when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set
                              # (set to false to force-disable even when keys are present)
```

Also add, just below the existing top-of-file provider comment block, a note documenting the env vars:

```yaml
# Langfuse tracing (optional, off unless keys are present):
#   Export LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to enable tracing of every
#   LLM call. LANGFUSE_OTEL_HOST selects region / self-hosted endpoint
#   (default US cloud; EU: https://cloud.langfuse.com). Disable via general.langfuse: false
#   or NAGARE_LANGFUSE=0.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 6: Mutation-catch check**

Temporarily set `"langfuse": False` in DEFAULTS; confirm the new test FAILS; revert; re-run PASS.

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/config.py config.example.yml tests/test_config.py
git commit -m "feat(tracing): general.langfuse config flag (default true) + docs"
```

---

### Task 7: Thread trace metadata through retry-loop stages (director, plan, summary)

**Files:**
- Modify: `src/nagare_clip/director/director_llm.py` (`generate_director_ops`)
- Modify: `src/nagare_clip/plan/plan_llm.py` (`generate_plan`)
- Modify: `src/nagare_clip/summary/summarize.py` (`segment_video`, `generate_project_summary`)
- Test: `tests/director/test_tracing_threading.py` (create), plus reuse existing per-stage test dirs

**Interfaces:**
- Consumes: `nagare_clip.llm_client.with_trace_meta`.
- Produces: each function wraps its `cfg` with `with_trace_meta(cfg, stage=recorder.stage, unit=<unit>)` once before the retry loop, so the injected `call_llm` receives `cfg["_trace"]`.

- [ ] **Step 1: Write the failing test**

Create `tests/director/test_tracing_threading.py`:

```python
from nagare_clip.director.director_llm import generate_director_ops
from nagare_clip.plan.plan_llm import generate_plan
from nagare_clip.summary.summarize import segment_video, generate_project_summary, PartSummary
from nagare_clip.plan.plan_llm import ProjectSummary
from nagare_clip.llm_report import Recorder


def _capturing_call_llm(store, response):
    def fake(messages, cfg):
        store.append(cfg)
        return response
    return fake


def test_director_threads_trace_meta():
    store = []
    rec = Recorder("director", None, enabled=False)
    generate_director_ops(
        ["one", "two"],
        {"prompt": "p", "max_retries": 0},
        call_llm=_capturing_call_llm(store, '{"ops": []}'),
        recorder=rec,
        unit="vidA",
    )
    assert store and store[0]["_trace"]["generation_name"] == "director/vidA"


def test_plan_threads_trace_meta():
    store = []
    rec = Recorder("plan", None, enabled=False)
    ps = ProjectSummary(summary="s", parts=[PartSummary(stem="v", lines=(1, 2), summary="x")])
    generate_plan(
        ps,
        {"prompt": "p", "max_retries": 0},
        call_llm=_capturing_call_llm(store, '{"directions": []}'),
        recorder=rec,
        unit="planU",
    )
    assert store and store[0]["_trace"]["tags"] == ["stage:plan", "stem:planU"]


def test_summary_segment_threads_trace_meta():
    store = []
    rec = Recorder("summary", None, enabled=False)
    segment_video(
        "stemX",
        ["a", "b"],
        {"prompt": "p", "max_retries": 0},
        call_llm=_capturing_call_llm(store, '{"parts": []}'),
        recorder=rec,
    )
    assert store and store[0]["_trace"]["generation_name"] == "summary/stemX"


def test_summary_overall_threads_trace_meta():
    store = []
    rec = Recorder("summary", None, enabled=False)
    generate_project_summary(
        [PartSummary(stem="v", lines=(1, 2), summary="x")],
        {"overall_prompt": "p", "max_retries": 0},
        call_llm=_capturing_call_llm(store, "a summary"),
        recorder=rec,
    )
    assert store and store[0]["_trace"]["generation_name"] == "summary/overall"
```

(If `ProjectSummary`/`PartSummary` import paths differ, fix the import to match the actual module — verify with `uv run python -c "from nagare_clip.summary.summarize import PartSummary, ProjectSummary"`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/director/test_tracing_threading.py -v`
Expected: FAIL — `KeyError: '_trace'`.

- [ ] **Step 3: Implement the wrapping**

Add the import to each of the three files (alongside their existing `from nagare_clip.text_filter.llm_filter import _call_llm` line):

```python
from nagare_clip.llm_client import with_trace_meta
```

In `director_llm.py` `generate_director_ops`, immediately before `attempts = retry_attempts(cfg)`:

```python
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
```

In `plan_llm.py` `generate_plan`, immediately before `attempts = retry_attempts(cfg)`:

```python
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
```

In `summarize.py` `segment_video`, immediately before `attempts = retry_attempts(cfg)`:

```python
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=stem)
```

In `summarize.py` `generate_project_summary`, immediately before `attempts = retry_attempts(cfg)`:

```python
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit="overall")
```

(`recorder.stage` is `""` for the disabled `Recorder` used in tests, but in production each CLI passes a stage-named recorder, e.g. `Recorder("director", ...)`. The test asserts the stage via the recorder it constructs.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/director/test_tracing_threading.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-catch check**

In `director_llm.py`, temporarily change `unit=unit` to `unit="wrong"`; confirm `test_director_threads_trace_meta` FAILS; revert; re-run PASS.

- [ ] **Step 6: Regression run**

Run: `uv run pytest tests/director tests/plan tests/summary -q`
Expected: PASS (the extra cfg key is ignored by existing fakes and by `call_llm`).

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/director/director_llm.py src/nagare_clip/plan/plan_llm.py src/nagare_clip/summary/summarize.py tests/director/test_tracing_threading.py
git commit -m "feat(tracing): thread trace metadata through director/plan/summary"
```

---

### Task 8: Thread trace metadata through single-call stages (text_filter, summary_llm, guided_edit)

**Files:**
- Modify: `src/nagare_clip/text_filter/llm_filter.py` (`_process_batch`)
- Modify: `src/nagare_clip/text_filter/summary_llm.py` (`generate_summary`)
- Modify: `src/nagare_clip/guided_edit/apply.py` (`apply_ops`)
- Test: `tests/guided_edit/test_tracing_threading.py` (create)

**Interfaces:**
- Consumes: `with_trace_meta`.
- Produces: each function wraps its `cfg` with `with_trace_meta` before issuing LLM calls, so the injected `call_llm` receives `cfg["_trace"]`.

- [ ] **Step 1: Write the failing test**

Create `tests/guided_edit/test_tracing_threading.py`:

```python
from nagare_clip.text_filter.llm_filter import _process_batch
from nagare_clip.text_filter.summary_llm import generate_summary
from nagare_clip.guided_edit.apply import apply_ops
from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.llm_report import Recorder


def _capture(store, response):
    def fake(messages, cfg):
        store.append(cfg)
        return response
    return fake


def test_text_filter_batch_threads_trace_meta():
    store = []
    rec = Recorder("text_filter", None, enabled=False)
    result = ["x", "y"]
    _process_batch(
        [(0, "x"), (1, "y")],
        result,
        {"prompt": "p"},
        2,
        None,
        call_llm=_capture(store, "1: x\n2: y"),
        recorder=rec,
    )
    assert store and store[0]["_trace"]["tags"][0] == "stage:text_filter"


def test_summary_llm_threads_trace_meta():
    store = []
    rec = Recorder("text_filter", None, enabled=False)
    generate_summary(
        "some transcript text",
        {"prompt": "p"},
        call_llm=_capture(store, '{"summary": "s", "keywords": []}'),
        recorder=rec,
    )
    assert store and store[0]["_trace"]["generation_name"] == "text_filter/summary_llm"


def test_guided_edit_threads_trace_meta():
    store = []
    rec = Recorder("guided_edit", None, enabled=False)
    op = DirectorOp(type="edit", lines=(1, 1), note="fix", factor=None, text=None)
    apply_ops(
        ["hello world"],
        [op],
        {"prompt": "p", "max_retries": 0},
        call_llm=_capture(store, "1: hello world"),
        recorder=rec,
        unit="vidB",
    )
    assert store and store[0]["_trace"]["generation_name"] == "guided_edit/vidB"
```

(Verify `DirectorOp`'s field names/constructor with `uv run python -c "from nagare_clip.director.director_llm import DirectorOp; print(DirectorOp.__dataclass_fields__.keys())"` and adjust the construction if needed.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/guided_edit/test_tracing_threading.py -v`
Expected: FAIL — `KeyError: '_trace'`.

- [ ] **Step 3: Implement the wrapping**

Add `from nagare_clip.llm_client import with_trace_meta` to each of the three files.

In `text_filter/llm_filter.py` `_process_batch`, after the line `unit = f"lines {a}-{b} (size {current_size})"` and before `messages = [`:

```python
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
```

In `text_filter/summary_llm.py` `generate_summary`, after the `if not full_text.strip(): return None` guard and before `messages = [`:

```python
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit="summary_llm")
```

In `guided_edit/apply.py` `apply_ops`, immediately before `attempts = retry_attempts(cfg)`:

```python
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/guided_edit/test_tracing_threading.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-catch check**

In `summary_llm.py`, temporarily change `unit="summary_llm"` to `unit="zzz"`; confirm `test_summary_llm_threads_trace_meta` FAILS; revert; re-run PASS.

- [ ] **Step 6: Regression run**

Run: `uv run pytest tests/text_filter tests/guided_edit -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/nagare_clip/text_filter/llm_filter.py src/nagare_clip/text_filter/summary_llm.py src/nagare_clip/guided_edit/apply.py tests/guided_edit/test_tracing_threading.py
git commit -m "feat(tracing): thread trace metadata through text_filter/guided_edit"
```

---

### Task 9: Per-run session id + config kill-switch in `run_pipeline.sh`

**Files:**
- Modify: `scripts/run_pipeline.sh`

**Interfaces:**
- Produces: every stage subprocess inherits `NAGARE_RUN_ID` (one timestamp per run) and `NAGARE_LANGFUSE` (`0` when `general.langfuse` is false).

- [ ] **Step 1: Export the run id**

In `scripts/run_pipeline.sh`, after the `PROJECT_ROOT=...` line (around line 7), add:

```bash
# One session id per pipeline run, inherited by every stage subprocess so
# Langfuse groups all of a run's LLM calls under one session.
export NAGARE_RUN_ID="${NAGARE_RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
```

- [ ] **Step 2: Read the kill-switch from config**

In the `eval "$(uv run ... python3 -c "..." )"` config block, add a `general` read. Inside the Python snippet, after `p  = c.get('pipeline', {})` add:

```python
g  = c.get('general', {})
```

and after the existing `out('CFG_TO_STAGE', ...)` line add:

```python
out('CFG_LANGFUSE', str(bool(g.get('langfuse', True))).lower())
```

- [ ] **Step 3: Apply the kill-switch**

After the precedence block (near the other `*_ENABLED` resolutions, e.g. after `AUDIO_SILENCE_MIN_SILENCE=...`), add:

```bash
LANGFUSE_ENABLED="${CFG_LANGFUSE:-true}"
if [[ "$LANGFUSE_ENABLED" == "false" ]]; then
  export NAGARE_LANGFUSE=0
fi
```

- [ ] **Step 4: Syntax check**

Run: `bash -n scripts/run_pipeline.sh`
Expected: no output (valid).

- [ ] **Step 5: Verify the env wiring without a full pipeline run**

Run:
```bash
NAGARE_RUN_ID= bash -c '
  source <(grep -n "" /dev/null);  # no-op
  export NAGARE_RUN_ID="${NAGARE_RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
  echo "run_id=$NAGARE_RUN_ID"
'
```
Expected: prints a `run_id=YYYYMMDD-HHMMSS` value (sanity check of the timestamp expression).

Then confirm the config read produces the flag:
```bash
printf 'general:\n  langfuse: false\n' > /tmp/lf_cfg.yml
uv run python3 -c "
import yaml
c = yaml.safe_load(open('/tmp/lf_cfg.yml'))
g = c.get('general', {})
print('CFG_LANGFUSE=' + str(bool(g.get('langfuse', True))).lower())
"
```
Expected: `CFG_LANGFUSE=false`.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_pipeline.sh
git commit -m "feat(tracing): export NAGARE_RUN_ID session + NAGARE_LANGFUSE kill-switch"
```

---

### Task 10: Documentation

**Files:**
- Modify: `README.md`
- Modify: `plan.md`
- Modify: `AGENTS.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: README — add a Langfuse tracing section**

Add a short "Observability / Langfuse tracing" subsection under the configuration/usage area describing: enabling via `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (+ `LANGFUSE_OTEL_HOST` for region/self-host), that it's off by default, that `general.langfuse: false` or `NAGARE_LANGFUSE=0` disables it, and that traces are grouped by run (`session_id`), stage, and source (`tags`). Note the markdown `llm_report` still runs alongside.

- [ ] **Step 2: plan.md — record status**

Add an entry noting Langfuse tracing was implemented at the `call_llm` layer via the `langfuse_otel` callback, env-gated, with run/stage/unit grouping, alongside `llm_report`.

- [ ] **Step 3: AGENTS.md — add guardrail under "Current Runtime Quirks" / Configuration System**

Add a bullet: all LLM calls route through `call_llm`, which (when `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` set and `NAGARE_LANGFUSE != 0` and `general.langfuse` true) registers LiteLLM's `langfuse_otel` callback once and attaches `metadata` (`generation_name=<stage>/<unit>`, `tags=[stage:…, stem:…]`, `session_id=NAGARE_RUN_ID`). Metadata is carried via `with_trace_meta` under a reserved `cfg["_trace"]` key, popped before the provider call. Note the short-lived-process `atexit` `flush_traces()` workaround and the documented fallback to the langfuse-SDK callback if OTEL flushing proves unreliable. Note this is observability-only and decoupled from any future LangGraph migration.

- [ ] **Step 4: Verify Python still compiles (hook parity)**

Run: `uv run python -m py_compile src/nagare_clip/llm_client.py src/nagare_clip/config.py`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add README.md plan.md AGENTS.md
git commit -m "docs: document Langfuse tracing integration"
```

---

### Task 11: Live verification gate (manual)

**Files:** none (verification only).

This task confirms the OTEL flush actually delivers traces from a short-lived stage process — the one risk unit tests cannot cover. Requires real Langfuse keys.

- [ ] **Step 1: Full suite green**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 2: Run one real LLM stage with tracing on**

With `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (and `LANGFUSE_OTEL_HOST` if non-US) exported, run a single enabled LLM stage against a sample — e.g. the director stage CLI on an existing `_edits.txt`, or the smallest enabled stage available:

```bash
NAGARE_RUN_ID="verify-$(date +%s)" \
  uv run python -m nagare_clip.director.cli --edits-txt output/guided_edit/<stem>_edits.txt --json output/transcription/<stem>.json --stem <stem>
```
(Use whatever single enabled LLM stage is convenient; the point is one real `call_llm` with keys set.)

- [ ] **Step 3: Confirm the trace landed in Langfuse**

Use the Langfuse CLI to verify a trace with the run's session id exists (set `LANGFUSE_HOST="$LANGFUSE_BASE_URL"` if needed):

```bash
npx langfuse-cli api traces list --help   # discover exact args
npx langfuse-cli api traces list          # then filter to the session/tags from this run
```
Expected: a trace whose generation carries `generation_name` like `director/<stem>`, `tags` `stage:director`/`stem:<stem>`, and the `session_id` you set. If nothing appears within a minute, the flush is failing → apply the spec's documented fallback (switch callback to the langfuse-SDK `["langfuse"]` integration with explicit `langfuse.flush()` in `flush_traces`) and re-verify.

- [ ] **Step 4: Confirm disabled path is silent**

Run the same stage with `NAGARE_LANGFUSE=0` and confirm no new trace appears. Expected: no trace created.

- [ ] **Step 5: Report results**

Summarize: full suite result, the trace observed (with its session/tags), and the disabled-path confirmation. Do not mark the feature complete without this evidence.

---

## Self-Review

- **Spec coverage:** mechanism/`langfuse_otel` (Tasks 4–5), env+config gating (Tasks 3, 6, 9), OTEL deps (Task 1), run+stage+unit grouping (Tasks 2, 7, 8, 9), `_trace`-via-cfg threading with untouched `call_llm` signature (Tasks 2, 5, 7, 8), llm_report left intact (no task modifies it), short-lived flush risk + verification gate + documented fallback (Tasks 4, 11), docs policy README/plan.md/AGENTS.md (Task 10). All spec sections map to a task.
- **Placeholder scan:** no TBD/TODO; every code step shows code; the only "use whatever is convenient" is in the manual verification task where the specific stem is environment-dependent, with an explicit command template.
- **Type consistency:** `with_trace_meta(cfg, *, stage, unit, extra_tags=())`, `_trace` key shape, `_tracing_enabled()`, `_ensure_tracing()`, `flush_traces()`, `LANGFUSE_OTEL_CALLBACK`, `_TRACING_INITIALIZED`, `NAGARE_RUN_ID`, `NAGARE_LANGFUSE` are used identically across all tasks.
