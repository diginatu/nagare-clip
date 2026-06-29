"""LLM layer for sentence_split: prompt, range parsing/validation, retry."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import (
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    UNPARSEABLE,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.text_filter.llm_filter import _call_llm

logger = logging.getLogger(__name__)

CallLLM = Callable[[List[Dict[str, str]], Dict[str, Any]], str]


def build_messages(
    bunsetsu: List[Tuple[int, int, str]], system_prompt: str
) -> List[Dict[str, str]]:
    listing = " ".join(f"{i}:{s}" for i, (_, _, s) in enumerate(bunsetsu))
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": listing},
    ]


def parse_ranges(response: str, num_bunsetsu: int) -> Optional[List[Tuple[int, int]]]:
    """Parse and validate ``{"sentences":[[a,b],…]}`` bunsetsu-index ranges.

    Returns the ranges only if they are contiguous, non-overlapping, and cover
    ``0..num_bunsetsu-1`` exactly; otherwise ``None``.
    """
    try:
        data = json.loads(response)
    except (json.JSONDecodeError, TypeError):
        return None
    raw = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    ranges: List[Tuple[int, int]] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(x, int) for x in item)
        ):
            return None
        a, b = item
        if a > b:
            return None
        ranges.append((a, b))
    if ranges[0][0] != 0 or ranges[-1][1] != num_bunsetsu - 1:
        return None
    for (_, b0), (a1, _) in zip(ranges, ranges[1:]):
        if a1 != b0 + 1:
            return None
    return ranges


def split_window(
    bunsetsu: List[Tuple[int, int, str]],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "window",
) -> Optional[List[Tuple[int, int]]]:
    """Return validated sentence ranges, or ``None`` to keep original segments.

    Retries (``max_retries``) on LLM exception or invalid ranges, nudging
    temperature each attempt.  A 0/1-bunsetsu window needs no LLM call.
    """
    num = len(bunsetsu)
    if num == 0:
        return []
    if num == 1:
        return [(0, 0)]
    messages = build_messages(bunsetsu, cfg.get("prompt", ""))
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "sentence_split LLM call failed (attempt %d/%d)", attempt + 1, attempts,
                exc_info=True,
            )
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                error=str(e), outcome=LLM_ERROR, reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        ranges = parse_ranges(response, num)
        if ranges is None:
            recorder.attempt(
                unit=unit, attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=UNPARSEABLE,
                reason="invalid/non-contiguous sentence ranges", cfg=attempt_cfg,
            )
            logger.warning(
                "sentence_split ranges invalid (attempt %d/%d)", attempt + 1, attempts
            )
            continue
        recorder.attempt(
            unit=unit, attempt=attempt, total=attempts, messages=messages,
            response=response, outcome=OK, reason="", cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=OK, reason="")
        return ranges
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning(
        "sentence_split: all %d attempt(s) failed; keeping original window", attempts
    )
    return None
