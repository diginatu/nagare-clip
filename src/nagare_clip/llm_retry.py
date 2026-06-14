"""Shared bounded-retry helpers for the LLM stages (director, guided_edit).

Both stages retry a failed LLM call/verification a bounded number of times,
nudging the sampling temperature upward on each successive attempt so a
deterministic low-temperature failure is not reproduced identically.
"""

from __future__ import annotations

from typing import Any, Dict

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_TEMP_STEP = 0.2
DEFAULT_RETRY_TEMP_CAP = 0.8


def retry_attempts(cfg: Dict[str, Any]) -> int:
    """Total attempts (first try + retries) for *cfg*.

    ``max_retries`` is the number of *extra* attempts after the first; a value
    of ``0`` (or negative) means a single attempt (today's behaviour).
    """
    extra = max(0, int(cfg.get("max_retries", DEFAULT_MAX_RETRIES)))
    return extra + 1


def cfg_for_attempt(cfg: Dict[str, Any], attempt: int) -> Dict[str, Any]:
    """Return *cfg* with ``temperature`` nudged for a 0-based *attempt* index.

    Attempt 0 keeps the configured temperature.  Each retry adds
    ``retry_temp_step``, capped at ``retry_temp_cap``.  The base *cfg* is never
    mutated.

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
    out["temperature"] = min(base + step * attempt, cap)
    return out
