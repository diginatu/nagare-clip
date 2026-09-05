"""Shared bounded-retry helpers for the LLM stages (director, guided_edit).

Both stages retry a failed LLM call/verification a bounded number of times,
nudging the sampling temperature upward on each successive attempt so a
deterministic low-temperature failure is not reproduced identically.
"""

from __future__ import annotations

from typing import Any

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_TEMP_STEP = 0.2
DEFAULT_RETRY_TEMP_CAP = 0.8


def retry_attempts(cfg: dict[str, Any]) -> int:
    """Total attempts (first try + retries) for *cfg*.

    ``max_retries`` is the number of *extra* attempts after the first; a value
    of ``0`` (or negative) means a single attempt (today's behaviour).
    """
    extra = max(0, int(cfg.get("max_retries", DEFAULT_MAX_RETRIES)))
    return extra + 1


def cfg_for_attempt(cfg: dict[str, Any], attempt: int) -> dict[str, Any]:
    """Return *cfg* with ``temperature`` nudged for a 0-based *attempt* index.

    Attempt 0 keeps the configured temperature.  Each retry adds
    ``retry_temp_step``, capped at ``retry_temp_cap``.  The base *cfg* is never
    mutated.

    The cap can only bound a **rise**: it never returns less than the
    configured temperature.  A retry that *lowered* it would contradict the
    word "adds", and it broke a real run -- a project on a model that accepts
    only ``temperature=1`` sets ``temperature: 1.0``, and the default cap of
    ``0.8`` handed every retry a value the provider rejected before the
    request left the machine.  For such a model, ``retry_temp_step: 0`` pins
    the temperature across all attempts.

    When no temperature is configured (absent or ``None``), there is nothing to
    nudge: the request rides the provider's own default on every attempt, so
    *cfg* is returned unchanged (the client never forwards a fabricated value).
    """
    if attempt <= 0:
        return cfg
    if cfg.get("temperature") is None:
        return cfg
    base = float(cfg["temperature"])
    step = float(cfg.get("retry_temp_step", DEFAULT_RETRY_TEMP_STEP))
    cap = float(cfg.get("retry_temp_cap", DEFAULT_RETRY_TEMP_CAP))
    out = dict(cfg)
    out["temperature"] = min(base + step * attempt, max(cap, base))
    return out
