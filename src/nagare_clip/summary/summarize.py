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
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    UNPARSEABLE,
    Recorder,
)
from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.text_filter.llm_filter import _call_llm

logger = logging.getLogger(__name__)

CallLLM = Callable[[List[Dict[str, str]], Dict[str, Any]], str]


@dataclass
class PartSummary:
    stem: str
    lines: Tuple[int, int]  # 1-based inclusive (start, end)
    summary: str
    start: Optional[float] = None  # part start time (s), from WhisperX segments
    end: Optional[float] = None    # part end time (s)


@dataclass
class ProjectSummary:
    summary: str
    parts: List[PartSummary] = field(default_factory=list)


def _strip_fence(response: str) -> str:
    text = response.strip()
    fence = _FENCE_RE.match(text)
    return fence.group(1) if fence else text


def _parse_parts_response(
    response: str, stem: str, num_lines: int, drops: Optional[List[str]] = None
) -> Optional[List[PartSummary]]:
    """Parse a ``{"parts": [{"lines": [a,b], "summary": "…"}]}`` response.

    Returns ``None`` on a hard parse failure (invalid JSON / no ``parts`` array)
    so the caller can retry; otherwise the (possibly empty) validated list, with
    malformed/out-of-range entries dropped (logged).
    """
    def _drop(msg: str) -> None:
        logger.warning("summary: %s", msg)
        if drops is not None:
            drops.append(msg)

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
            _drop(f"part dropped, bad lines {raw.get('lines')!r}")
            continue
        summary = raw.get("summary")
        if not isinstance(summary, str) or summary == "":
            _drop("part dropped, empty/missing summary")
            continue
        parts.append(PartSummary(stem=stem, lines=lines, summary=summary))
    return parts


def segment_video(
    stem: str,
    clean_lines: List[str],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> List[PartSummary]:
    """Segment one video's transcript into summarised parts (line ranges)."""
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": format_numbered_transcript(clean_lines)},
    ]
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=stem)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "summary: segment LLM call failed (attempt %d/%d) for %s",
                attempt + 1, attempts, stem, exc_info=True,
            )
            recorder.attempt(
                unit=stem, attempt=attempt, total=attempts, messages=messages,
                error=str(e), outcome=LLM_ERROR, reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        drops: List[str] = []
        parts = _parse_parts_response(
            response, stem, num_lines=len(clean_lines), drops=drops
        )
        if parts is None:
            recorder.attempt(
                unit=stem, attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=UNPARSEABLE,
                reason="invalid JSON / no 'parts' array", cfg=attempt_cfg,
            )
            logger.warning(
                "summary: segment response unparseable (attempt %d/%d) for %s",
                attempt + 1, attempts, stem,
            )
            continue
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)
        elif not parts:
            outcome, reason = OK_EMPTY, ""
        else:
            outcome, reason = OK, ""
        recorder.attempt(
            unit=stem, attempt=attempt, total=attempts, messages=messages,
            response=response, outcome=outcome, reason=reason, cfg=attempt_cfg,
        )
        recorder.flush_unit(stem, outcome=outcome, reason=reason)
        return parts
    recorder.flush_unit(stem, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
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
    recorder: Recorder = NULL_RECORDER,
) -> str:
    """Synthesise a single all-videos summary from the per-part summaries."""
    if not parts:
        return ""
    messages = [
        {"role": "system", "content": cfg.get("overall_prompt", "")},
        {"role": "user", "content": _format_parts_doc(parts)},
    ]
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit="overall")
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "summary: overall LLM call failed (attempt %d/%d)",
                attempt + 1,
                attempts,
                exc_info=True,
            )
            recorder.attempt(
                unit="overall", attempt=attempt, total=attempts, messages=messages,
                error=str(e), outcome=LLM_ERROR, reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        summary = _parse_overall_response(response)
        if summary is None:
            recorder.attempt(
                unit="overall", attempt=attempt, total=attempts, messages=messages,
                response=response, outcome=UNPARSEABLE, reason="no 'summary' string",
                cfg=attempt_cfg,
            )
            logger.warning(
                "summary: overall response unparseable (attempt %d/%d)",
                attempt + 1,
                attempts,
            )
            continue
        outcome = OK_EMPTY if summary == "" else OK
        recorder.attempt(
            unit="overall", attempt=attempt, total=attempts, messages=messages,
            response=response, outcome=outcome, cfg=attempt_cfg,
        )
        recorder.flush_unit("overall", outcome=outcome)
        return summary
    recorder.flush_unit("overall", outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("summary: all %d overall attempt(s) failed; empty summary", attempts)
    return ""


def _attach_part_times(
    part: PartSummary,
    seg_times: Optional[List[Tuple[Optional[float], Optional[float]]]],
) -> None:
    """Set ``part.start``/``part.end`` from segment times for its line range."""
    if not seg_times:
        return
    a, b = part.lines
    if not (1 <= a <= b <= len(seg_times)):
        return
    part.start = seg_times[a - 1][0]
    part.end = seg_times[b - 1][1]


def build_summary(
    parts_input: List[Tuple[str, List[str]]],
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    seg_times_by_stem: Optional[
        Dict[str, List[Tuple[Optional[float], Optional[float]]]]
    ] = None,
) -> ProjectSummary:
    """Map (``segment_video`` per video) then reduce (``generate_project_summary``)."""
    parts: List[PartSummary] = []
    for stem, clean_lines in parts_input:
        parts.extend(
            segment_video(stem, clean_lines, cfg, call_llm=call_llm, recorder=recorder)
        )
    if seg_times_by_stem:
        for p in parts:
            _attach_part_times(p, seg_times_by_stem.get(p.stem))
    summary = generate_project_summary(parts, cfg, call_llm=call_llm, recorder=recorder)
    return ProjectSummary(summary=summary, parts=parts)


def summary_to_dict(ps: ProjectSummary) -> Dict[str, Any]:
    parts: List[Dict[str, Any]] = []
    for p in ps.parts:
        entry: Dict[str, Any] = {
            "stem": p.stem,
            "lines": [p.lines[0], p.lines[1]],
            "summary": p.summary,
        }
        if p.start is not None:
            entry["start"] = p.start
        if p.end is not None:
            entry["end"] = p.end
        parts.append(entry)
    return {"summary": ps.summary, "parts": parts}


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
            start = raw.get("start")
            end = raw.get("end")
            start = float(start) if isinstance(start, (int, float)) else None
            end = float(end) if isinstance(end, (int, float)) else None
            parts.append(
                PartSummary(stem=stem, lines=lines, summary=s, start=start, end=end)
            )
    return ProjectSummary(summary=summary, parts=parts)
