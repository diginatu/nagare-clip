"""Unified LLM client backed by LiteLLM.

Routes every stage's chat call through ``litellm.completion`` so a stage can
target OpenAI, Gemini, Anthropic, or a local Ollama purely from config. The
provider is chosen by ``cfg['provider']`` (a LiteLLM prefix, default
``"ollama_chat"``); the model id handed to LiteLLM is ``f"{provider}/{model}"``.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from collections.abc import Iterable
from typing import Any

import litellm

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_API_BASE = "http://localhost:11434"

LANGFUSE_OTEL_CALLBACK = "langfuse_otel"
_TRACING_INITIALIZED = False


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
    # Process-global, not thread-safe: the pipeline runs each stage as its own
    # process, so the check-then-set below never races. If a future stage calls
    # call_llm concurrently from threads, guard this with a lock.
    if not _TRACING_INITIALIZED:
        callbacks = list(getattr(litellm, "callbacks", []) or [])
        if LANGFUSE_OTEL_CALLBACK not in callbacks:
            callbacks.append(LANGFUSE_OTEL_CALLBACK)
            litellm.callbacks = callbacks
        atexit.register(flush_traces)
        _TRACING_INITIALIZED = True
    return True


def _tracing_enabled() -> bool:
    """Whether Langfuse tracing should be active for this process."""
    if os.environ.get("NAGARE_LANGFUSE", "1") == "0":
        return False
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY")) and bool(
        os.environ.get("LANGFUSE_SECRET_KEY")
    )


def with_trace_meta(
    cfg: dict[str, Any],
    *,
    stage: str,
    unit: str,
    extra_tags: Iterable[str] = (),
) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Usage (token counts) hand-off
#
# ``CallLLM`` is ``Callable[[list[dict], dict], str]`` and every stage plus
# every test fake depends on a plain ``str`` coming back, so the provider's
# usage numbers cannot ride the return value.  They are parked in a
# thread-local slot instead and picked up by ``llm_report.Recorder.attempt``,
# which is the only caller that wants them.  Thread-local (rather than a plain
# module global) so a future threaded stage cannot attribute one call's tokens
# to another's report entry.  The slot is cleared at the START of every call,
# so a failed request can never leave a stale count behind, and
# :func:`take_last_usage` consumes it, so an attempt recorded without an LLM
# call (guided_edit's deterministic verification) records no usage.
# ---------------------------------------------------------------------------

_last_usage = threading.local()


def _set_last_usage(usage: dict[str, int] | None) -> None:
    _last_usage.value = usage


def take_last_usage() -> dict[str, int] | None:
    """Pop the usage recorded by the most recent :func:`call_llm` on this thread."""
    value = getattr(_last_usage, "value", None)
    _last_usage.value = None
    return value


def _usage_field(obj: Any, key: str) -> Any:
    """Read *key* off a usage object that may be a pydantic model or a dict."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def extract_usage(response: Any) -> dict[str, int] | None:
    """Normalise ``response.usage`` into a flat ``{name: count}`` mapping.

    Returns ``None`` when the provider (or a test fake) reports no usage at
    all — a missing field is "no measurement", never an error.

    LiteLLM normalises Anthropic's ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens`` onto ``prompt_tokens_details`` as
    ``cached_tokens`` / ``cache_creation_tokens`` and folds both into
    ``prompt_tokens`` (see ``litellm/types/utils.py`` ``Usage.__init__`` and
    ``litellm/llms/anthropic/chat/transformation.py``), so ``prompt_tokens``
    is the *total* input including the part served from cache.  The raw
    Anthropic names are read too, for a provider or fake that passes them
    through untouched.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _int_or_none(_usage_field(usage, key))
        if value is not None:
            out[key] = value

    prompt_details = _usage_field(usage, "prompt_tokens_details")
    for out_key, candidates in (
        ("cache_read_input_tokens", ("cached_tokens",)),
        ("cache_creation_input_tokens", ("cache_creation_tokens",)),
    ):
        value = None
        for candidate in candidates:
            value = _int_or_none(_usage_field(prompt_details, candidate))
            if value is not None:
                break
        if value is None:
            value = _int_or_none(_usage_field(usage, out_key))
        if value is not None:
            out[out_key] = value

    completion_details = _usage_field(usage, "completion_tokens_details")
    reasoning = _int_or_none(_usage_field(completion_details, "reasoning_tokens"))
    if reasoning is not None:
        out["reasoning_tokens"] = reasoning

    return out or None


# ---------------------------------------------------------------------------
# Anthropic prompt caching
#
# The director sends a byte-identical ~3.8k-token system prompt on every one of
# its calls and a different user message each time, so the system message is
# exactly the "shared prefix, varying suffix" shape prompt caching is for: one
# breakpoint at the end of the shared part bills calls 2..N as cache reads
# (~0.1x input price) instead of full-rate input.
#
# Anthropic's constraints that matter here:
#   * a cache entry is only created once the marked prefix reaches a
#     model-dependent minimum token count; below it the marker is silently
#     ignored (see ANTHROPIC_MIN_CACHEABLE_TOKENS),
#   * at most 4 cache_control breakpoints per request — we use exactly one,
#   * the default TTL is 5 minutes, refreshed by every read, which suits a run
#     whose calls follow each other closely.  A 1h TTL costs 2x on the write
#     and would need a config key this module does not own.
#
# LiteLLM expresses the breakpoint as a ``cache_control`` key on a system
# message content BLOCK (litellm/llms/anthropic/chat/transformation.py,
# ``translate_system_message``), so the string content is rewritten into a
# one-element text-block list.  Anything but the anthropic provider is left
# byte-identical.
# ---------------------------------------------------------------------------

CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}

# Minimum cacheable prefix per model, longest-prefix match on the model id.
# Not monotonic across generations, so it has to be a table rather than a rule.
ANTHROPIC_MIN_CACHEABLE_TOKENS = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-mythos-5": 512,
    "claude-sonnet-5": 1024,
    "claude-opus-4-8": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-sonnet-4-5": 1024,
    "claude-sonnet-4": 1024,
    "claude-opus-4-1": 1024,
    "claude-opus-4": 1024,
    "claude-opus-4-7": 2048,
    "claude-haiku-3-5": 2048,
    "claude-opus-4-6": 4096,
    "claude-opus-4-5": 4096,
    "claude-haiku-4-5": 4096,
}
# An unrecognised model gets the largest minimum any current model asks for, so
# an unknown id can only ever skip caching, never mark a prefix too short to
# earn its cache-write premium.
DEFAULT_MIN_CACHEABLE_TOKENS = 4096


def min_cacheable_tokens(model: str) -> int:
    """Smallest prefix Anthropic will cache for *model* (longest-prefix match)."""
    best = ""
    for name in ANTHROPIC_MIN_CACHEABLE_TOKENS:
        if model.startswith(name) and len(name) > len(best):
            best = name
    return ANTHROPIC_MIN_CACHEABLE_TOKENS.get(best, DEFAULT_MIN_CACHEABLE_TOKENS)


def _count_tokens(text: str, model: str) -> int:
    """Approximate token count, erring low so the gate skips rather than guesses."""
    try:
        return int(litellm.token_counter(model=f"anthropic/{model}", text=text))
    except Exception:  # noqa: BLE001 - a counting failure must not break the call
        logger.debug("token_counter failed; falling back to a char estimate", exc_info=True)
        return len(text) // 4


#: Message key a caller sets on a system message to say how much of its content
#: is identical on every call.  The breakpoint goes at the end of that prefix,
#: not at the end of the message: Anthropic caches by exact prefix, so a
#: breakpoint placed after a per-call tail makes every call's prefix unique —
#: each one pays the cache-write premium and none ever reads.  (The director's
#: nine system messages share an 8.7k-character head and all differ after it.)
#: Stripped before any provider sees it.
CACHEABLE_PREFIX_KEY = "cacheable_prefix"


def _strip_cache_hint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*messages* without the :data:`CACHEABLE_PREFIX_KEY` hint, caller's list untouched."""
    if not any(CACHEABLE_PREFIX_KEY in m for m in messages):
        return messages
    return [{k: v for k, v in m.items() if k != CACHEABLE_PREFIX_KEY} for m in messages]


def _with_cache_control(messages: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    """Return *messages* with one ``cache_control`` breakpoint on the system prefix.

    The breakpoint ends the stable prefix: the system message's
    :data:`CACHEABLE_PREFIX_KEY` when it declares one — the content is then
    sent as two text blocks, the first marked — and the whole message when it
    does not.  A declared prefix the content does not start with places no
    breakpoint at all: a misplaced one costs the write premium for nothing.

    A no-op (the original list object) when there is no plain-string system
    message, or when the prefix is below *model*'s cacheable minimum.  Never
    mutates the caller's list or its dicts: the stages reuse the same message
    list across retry attempts and hand it to the LLM report verbatim.
    """
    last_system = -1
    for i, message in enumerate(messages):
        if message.get("role") == "system":
            last_system = i
    if last_system < 0:
        return messages

    # A caller that already built content blocks owns its own breakpoints.
    # (An empty string needs no separate guard: it can never clear the token
    # minimum below.)
    content = messages[last_system].get("content")
    if not isinstance(content, str):
        return messages

    prefix = messages[last_system].get(CACHEABLE_PREFIX_KEY, content)
    if not isinstance(prefix, str) or not content.startswith(prefix):
        logger.warning("declared cacheable prefix does not start the system message; not marking")
        return messages

    if _count_tokens(prefix, model) < min_cacheable_tokens(model):
        logger.debug("system prefix below %s's cacheable minimum; not marking", model)
        return messages

    blocks = [{"type": "text", "text": prefix, "cache_control": dict(CACHE_CONTROL_EPHEMERAL)}]
    if content[len(prefix) :]:
        blocks.append({"type": "text", "text": content[len(prefix) :]})
    out = list(messages)
    marked = dict(messages[last_system])
    marked["content"] = blocks
    out[last_system] = marked
    return out


def call_llm(messages: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    """Send chat ``messages`` to the configured provider, return text content.

    Falls back to the local Ollama base URL when an Ollama provider is selected
    with an empty ``api_base``. Any LiteLLM error is re-raised as
    ``ConnectionError`` so the stages' existing broad-``Exception`` retry loops
    treat it like the old urllib transport did.
    """
    _set_last_usage(None)
    cfg = dict(cfg)
    trace = cfg.pop("_trace", None)
    provider = cfg.get("provider", "ollama_chat")
    model = cfg.get("model", "")

    if provider == "anthropic":
        messages = _with_cache_control(messages, model)
    messages = _strip_cache_hint(messages)

    kwargs: dict[str, Any] = {
        "model": f"{provider}/{model}",
        "messages": messages,
        "timeout": cfg.get("timeout", 300),
    }

    # Forward temperature only when set, so a provider's own default applies
    # when it is omitted/null (and temperature-restricted reasoning models do
    # not error on a forced value).  The retry ladder in ``llm_retry`` follows
    # the same opt-in rule, so an unset temperature rides the default on every
    # attempt rather than being fabricated on retry.
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

    _set_last_usage(extract_usage(response))
    return response.choices[0].message.content
