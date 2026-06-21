"""Summary LLM: generates transcript summary and keywords for the filter LLM."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.IGNORECASE)

from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import LLM_ERROR, NULL_RECORDER, OK, UNPARSEABLE, Recorder
from nagare_clip.text_filter.llm_filter import _call_llm

logger = logging.getLogger(__name__)


@dataclass
class SummaryResult:
    summary: str
    keywords: List[str]


def parse_summary_response(response: str) -> SummaryResult | None:
    """Parse JSON response with ``summary`` and ``keywords`` fields."""
    if isinstance(response, str):
        m = _FENCE_RE.match(response)
        if m:
            response = m.group(1)
    try:
        data = json.loads(response)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    summary = data.get("summary")
    keywords = data.get("keywords")

    if not isinstance(summary, str) or not isinstance(keywords, list):
        return None

    return SummaryResult(
        summary=summary,
        keywords=[str(k).strip() for k in keywords],
    )


def build_enhanced_prompt(base_prompt: str, summary: SummaryResult) -> str:
    """Append summary and keywords context to the filter LLM's base prompt."""
    if not summary.summary and not summary.keywords:
        return base_prompt
    parts = [base_prompt, "", "Context about this transcript:"]
    if summary.summary:
        parts.append(f"Summary: {summary.summary}")
    if summary.keywords:
        kw_str = ", ".join(summary.keywords)
        parts.append(f"Keywords (correct spellings): {kw_str}")
        parts.append(
            "When you see words that sound similar to these keywords, "
            "correct them."
        )
    return "\n".join(parts)


def generate_summary(
    full_text: str,
    cfg: Dict[str, Any],
    *,
    call_llm=None,
    recorder: Recorder = NULL_RECORDER,
) -> SummaryResult | None:
    """Call the summary LLM and return parsed result, or None on failure."""
    if call_llm is None:
        call_llm = _call_llm
    if not full_text.strip():
        return None
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit="summary_llm")
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": full_text},
    ]

    try:
        response = call_llm(messages, cfg)
    except Exception as e:  # noqa: BLE001 - recoverable
        logger.warning("Summary LLM call failed, proceeding without summary", exc_info=True)
        recorder.attempt(
            unit="summary_llm", attempt=0, total=1, messages=messages, error=str(e),
            outcome=LLM_ERROR, reason="LLM call failed", cfg=cfg,
        )
        recorder.flush_unit("summary_llm", outcome=LLM_ERROR, reason="LLM call failed")
        return None

    result = parse_summary_response(response)
    if result is None:
        logger.warning("Failed to parse summary LLM response: %s", response[:200])
        recorder.attempt(
            unit="summary_llm", attempt=0, total=1, messages=messages, response=response,
            outcome=UNPARSEABLE, reason="unparseable summary JSON", cfg=cfg,
        )
        recorder.flush_unit("summary_llm", outcome=UNPARSEABLE, reason="unparseable summary JSON")
        return result
    recorder.attempt(
        unit="summary_llm", attempt=0, total=1, messages=messages, response=response,
        outcome=OK, cfg=cfg,
    )
    recorder.flush_unit("summary_llm", outcome=OK)
    return result
