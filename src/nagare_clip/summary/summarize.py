"""summary stage: segment each transcript into line-range parts + summarise.

Runs once project-wide (over all videos). For each video the LLM segments the
numbered transcript into parts (line ranges) and writes a summary per part
(``segment_video``); a reduce step then synthesises a single all-videos summary
(``generate_project_summary``). The result is written to ``summary.json`` for
human review and consumed by the ``plan`` and ``director`` stages.

Graceful by design: any LLM/parse failure degrades to an empty part list / empty
summary, so the pipeline proceeds.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from nagare_clip.director.director_llm import (
    _FENCE_RE,
    _coerce_lines,
    format_numbered_transcript,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.stage2.llm_filter import _call_llm

logger = logging.getLogger(__name__)

CallLLM = Callable[[List[Dict[str, str]], Dict[str, Any]], str]


@dataclass
class PartSummary:
    stem: str
    lines: Tuple[int, int]  # 1-based inclusive (start, end)
    summary: str


@dataclass
class ProjectSummary:
    summary: str
    parts: List[PartSummary] = field(default_factory=list)


def _strip_fence(response: str) -> str:
    text = response.strip()
    fence = _FENCE_RE.match(text)
    return fence.group(1) if fence else text


def _parse_parts_response(
    response: str, stem: str, num_lines: int
) -> Optional[List[PartSummary]]:
    """Parse a ``{"parts": [{"lines": [a,b], "summary": "…"}]}`` response.

    Returns ``None`` on a hard parse failure (invalid JSON / no ``parts`` array)
    so the caller can retry; otherwise the (possibly empty) validated list, with
    malformed/out-of-range entries dropped (logged).
    """
    try:
        data = json.loads(_strip_fence(response))
    except (ValueError, TypeError):
        logger.warning("summary: parts response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("parts"), list):
        logger.warning("summary: parts response has no 'parts' array; ignoring")
        return None

    parts: List[PartSummary] = []
    for raw in data["parts"]:
        if not isinstance(raw, dict):
            continue
        lines = _coerce_lines(raw.get("lines"), num_lines)
        if lines is None:
            logger.warning("summary: part dropped, bad lines %r", raw.get("lines"))
            continue
        summary = raw.get("summary")
        if not isinstance(summary, str) or summary == "":
            logger.warning("summary: part dropped, empty/missing summary")
            continue
        parts.append(PartSummary(stem=stem, lines=lines, summary=summary))
    return parts


def segment_video(
    stem: str,
    clean_lines: List[str],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
) -> List[PartSummary]:
    """Segment one video's transcript into summarised parts (line ranges)."""
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": format_numbered_transcript(clean_lines)},
    ]
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        try:
            response = call_llm(messages, cfg_for_attempt(cfg, attempt))
        except Exception:
            logger.warning(
                "summary: segment LLM call failed (attempt %d/%d) for %s",
                attempt + 1,
                attempts,
                stem,
                exc_info=True,
            )
            continue
        parts = _parse_parts_response(response, stem, num_lines=len(clean_lines))
        if parts is not None:
            return parts
        logger.warning(
            "summary: segment response unparseable (attempt %d/%d) for %s",
            attempt + 1,
            attempts,
            stem,
        )
    logger.warning("summary: all %d attempt(s) failed for %s; no parts", attempts, stem)
    return []


def _format_parts_doc(parts: List[PartSummary]) -> str:
    return "\n".join(
        f"{i + 1}: {p.stem} [{p.lines[0]}-{p.lines[1]}] — {p.summary}"
        for i, p in enumerate(parts)
    )


def _parse_overall_response(response: str) -> Optional[str]:
    try:
        data = json.loads(_strip_fence(response))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    summary = data.get("summary")
    return summary if isinstance(summary, str) else None


def generate_project_summary(
    parts: List[PartSummary],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
) -> str:
    """Synthesise a single all-videos summary from the per-part summaries."""
    if not parts:
        return ""
    messages = [
        {"role": "system", "content": cfg.get("overall_prompt", "")},
        {"role": "user", "content": _format_parts_doc(parts)},
    ]
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        try:
            response = call_llm(messages, cfg_for_attempt(cfg, attempt))
        except Exception:
            logger.warning(
                "summary: overall LLM call failed (attempt %d/%d)",
                attempt + 1,
                attempts,
                exc_info=True,
            )
            continue
        summary = _parse_overall_response(response)
        if summary is not None:
            return summary
        logger.warning(
            "summary: overall response unparseable (attempt %d/%d)",
            attempt + 1,
            attempts,
        )
    logger.warning("summary: all %d overall attempt(s) failed; empty summary", attempts)
    return ""


def build_summary(
    parts_input: List[Tuple[str, List[str]]],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
) -> ProjectSummary:
    """Map (``segment_video`` per video) then reduce (``generate_project_summary``)."""
    parts: List[PartSummary] = []
    for stem, clean_lines in parts_input:
        parts.extend(segment_video(stem, clean_lines, cfg, call_llm=call_llm))
    summary = generate_project_summary(parts, cfg, call_llm=call_llm)
    return ProjectSummary(summary=summary, parts=parts)


def summary_to_dict(ps: ProjectSummary) -> Dict[str, Any]:
    return {
        "summary": ps.summary,
        "parts": [
            {"stem": p.stem, "lines": [p.lines[0], p.lines[1]], "summary": p.summary}
            for p in ps.parts
        ],
    }


def _coerce_pair(value: Any) -> Optional[Tuple[int, int]]:
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        return None
    a, b = value
    if isinstance(a, bool) or isinstance(b, bool):
        return None
    if not isinstance(a, int) or not isinstance(b, int):
        return None
    return (a, b)


def summary_from_dict(data: Any) -> ProjectSummary:
    if not isinstance(data, dict):
        return ProjectSummary(summary="", parts=[])
    summary = data.get("summary")
    summary = summary if isinstance(summary, str) else ""
    parts: List[PartSummary] = []
    raw_parts = data.get("parts")
    if isinstance(raw_parts, list):
        for raw in raw_parts:
            if not isinstance(raw, dict):
                continue
            stem = raw.get("stem")
            lines = _coerce_pair(raw.get("lines"))
            s = raw.get("summary")
            if not isinstance(stem, str) or lines is None or not isinstance(s, str):
                continue
            parts.append(PartSummary(stem=stem, lines=lines, summary=s))
    return ProjectSummary(summary=summary, parts=parts)
