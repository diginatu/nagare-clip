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
from typing import Any, Dict, Iterable, List

import litellm

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_API_BASE = "http://localhost:11434"

LANGFUSE_OTEL_CALLBACK = "langfuse_otel"
_TRACING_INITIALIZED = False


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

    logger.debug("LLM request model=%s", kwargs["model"])
    try:
        response = litellm.completion(**kwargs)
    except Exception as e:  # noqa: BLE001 - normalize for the stages' retry loops
        raise ConnectionError(f"LLM API request failed: {e}") from e

    return response.choices[0].message.content
