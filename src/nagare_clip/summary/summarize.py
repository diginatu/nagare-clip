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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nagare_clip.director.director_llm import (
    _FENCE_RE,
    _coerce_lines,
    format_numbered_transcript,
)
from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    UNPARSEABLE,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.text_filter.llm_filter import _call_llm
from nagare_clip.timing import span_silence

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, str]], dict[str, Any]], str]


@dataclass
class PartSummary:
    stem: str
    lines: tuple[int, int]  # 1-based inclusive (start, end)
    summary: str
    start: float | None = None  # part start time (s), from WhisperX segments
    end: float | None = None  # part end time (s)
    silence: float | None = None  # audio_silence-cut seconds inside [start, end]


@dataclass
class ProjectSummary:
    summary: str
    parts: list[PartSummary] = field(default_factory=list)
    keywords: dict[str, list[str]] = field(default_factory=dict)  # stem -> correct spellings
    video_summaries: dict[str, str] = field(default_factory=dict)  # stem -> whole-video summary


def _strip_fence(response: str) -> str:
    text = response.strip()
    fence = _FENCE_RE.match(text)
    return fence.group(1) if fence else text


def _coerce_keywords(value: Any) -> list[str]:
    """Lenient keyword list: keep stripped non-empty strings, drop everything else."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _parse_parts_response(
    response: str, stem: str, num_lines: int, drops: list[str] | None = None
) -> tuple[list[PartSummary], list[str], str] | None:
    """Parse ``{"parts": [...], "keywords": [...], "video_summary": "…"}``.

    Returns ``None`` on a hard parse failure — invalid JSON, no ``parts`` array,
    or a missing/non-string/empty ``video_summary`` — so the caller can retry.
    Otherwise ``(parts, keywords, video_summary)`` where ``parts`` is the
    validated (possibly empty) list with malformed entries dropped (logged).
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
    video_summary = data.get("video_summary")
    if not isinstance(video_summary, str) or not video_summary.strip():
        logger.warning("summary: parts response has no 'video_summary'; ignoring")
        return None

    parts: list[PartSummary] = []
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
    return parts, _coerce_keywords(data.get("keywords")), video_summary.strip()


def segment_video(
    stem: str,
    clean_lines: list[str],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    gap_block: str = "",
) -> tuple[list[PartSummary], list[str], str]:
    """Segment one video's transcript into summarised parts, misspelling-prone
    keywords, and a whole-video summary.

    ``gap_block`` (from the gap_context stage) describes what is visible during
    this video's long silences; appended to the user content when non-empty, so
    an absent/empty block leaves the prompt byte-identical.
    """
    user_content = format_numbered_transcript(clean_lines)
    if gap_block:
        user_content = f"{user_content}\n\n{gap_block}"
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": user_content},
    ]
    recorder.begin(stem)
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=stem)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "summary: segment LLM call failed (attempt %d/%d) for %s",
                attempt + 1,
                attempts,
                stem,
                exc_info=True,
            )
            recorder.attempt(
                unit=stem,
                attempt=attempt,
                total=attempts,
                messages=messages,
                error=str(e),
                outcome=LLM_ERROR,
                reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        drops: list[str] = []
        parsed = _parse_parts_response(response, stem, num_lines=len(clean_lines), drops=drops)
        if parsed is None:
            recorder.attempt(
                unit=stem,
                attempt=attempt,
                total=attempts,
                messages=messages,
                response=response,
                outcome=UNPARSEABLE,
                reason="invalid JSON / no 'parts' array",
                cfg=attempt_cfg,
            )
            logger.warning(
                "summary: segment response unparseable (attempt %d/%d) for %s",
                attempt + 1,
                attempts,
                stem,
            )
            continue
        parts, keywords, video_summary = parsed
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)
        elif not parts:
            outcome, reason = OK_EMPTY, ""
        else:
            outcome, reason = OK, ""
        recorder.attempt(
            unit=stem,
            attempt=attempt,
            total=attempts,
            messages=messages,
            response=response,
            outcome=outcome,
            reason=reason,
            cfg=attempt_cfg,
        )
        recorder.flush_unit(stem, outcome=outcome, reason=reason)
        return parts, keywords, video_summary
    recorder.flush_unit(stem, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("summary: all %d attempt(s) failed for %s; no parts", attempts, stem)
    return [], [], ""


def _format_parts_doc(parts: list[PartSummary], video_summaries: dict[str, str]) -> str:
    """Render parts grouped under per-video ``## <stem> — <video summary>`` headers,
    keeping global 1-based numbering across the whole document."""
    lines: list[str] = []
    current: str | None = None
    for i, p in enumerate(parts):
        if p.stem != current:
            current = p.stem
            # Via build_summary every stem in parts has a non-empty video summary;
            # the bare-header fallback is defensive for direct callers that pass a
            # video_summaries dict missing this stem.
            vs = video_summaries.get(p.stem, "")
            lines.append(f"## {p.stem} — {vs}" if vs else f"## {p.stem}")
        lines.append(f"{i + 1}: [{p.lines[0]}-{p.lines[1]}] — {p.summary}")
    return "\n".join(lines)


def _parse_overall_response(response: str) -> str | None:
    try:
        data = json.loads(_strip_fence(response))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    summary = data.get("summary")
    return summary if isinstance(summary, str) else None


def generate_project_summary(
    parts: list[PartSummary],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    video_summaries: dict[str, str] | None = None,
) -> str:
    """Synthesise a single all-videos summary from the per-part summaries."""
    if not parts:
        return ""
    messages = [
        {"role": "system", "content": cfg.get("overall_prompt", "")},
        {"role": "user", "content": _format_parts_doc(parts, video_summaries or {})},
    ]
    recorder.begin("overall")
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
                unit="overall",
                attempt=attempt,
                total=attempts,
                messages=messages,
                error=str(e),
                outcome=LLM_ERROR,
                reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        summary = _parse_overall_response(response)
        if summary is None:
            recorder.attempt(
                unit="overall",
                attempt=attempt,
                total=attempts,
                messages=messages,
                response=response,
                outcome=UNPARSEABLE,
                reason="no 'summary' string",
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
            unit="overall",
            attempt=attempt,
            total=attempts,
            messages=messages,
            response=response,
            outcome=outcome,
            cfg=attempt_cfg,
        )
        recorder.flush_unit("overall", outcome=outcome)
        return summary
    recorder.flush_unit("overall", outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("summary: all %d overall attempt(s) failed; empty summary", attempts)
    return ""


def _attach_part_times(
    part: PartSummary,
    seg_times: list[tuple[float | None, float | None]] | None,
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
    parts_input: list[tuple[str, list[str]]],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    seg_times_by_stem: dict[str, list[tuple[float | None, float | None]]] | None = None,
    gap_blocks_by_stem: dict[str, str] | None = None,
    cuts_by_stem: dict[str, list[tuple[float, float]]] | None = None,
) -> ProjectSummary:
    """Map (``segment_video`` per video) then reduce (``generate_project_summary``)."""
    parts: list[PartSummary] = []
    keywords: dict[str, list[str]] = {}
    video_summaries: dict[str, str] = {}
    for stem, clean_lines in parts_input:
        video_parts, video_keywords, video_summary = segment_video(
            stem,
            clean_lines,
            cfg,
            call_llm=call_llm,
            recorder=recorder,
            gap_block=(gap_blocks_by_stem or {}).get(stem, ""),
        )
        parts.extend(video_parts)
        if video_keywords:
            keywords[stem] = video_keywords
        if video_summary:
            video_summaries[stem] = video_summary
    if seg_times_by_stem:
        for p in parts:
            _attach_part_times(p, seg_times_by_stem.get(p.stem))
    if cuts_by_stem:
        for p in parts:
            cuts = cuts_by_stem.get(p.stem)
            if cuts and p.start is not None and p.end is not None:
                sil = span_silence(p.start, p.end, cuts)
                if sil > 0.0:
                    p.silence = sil
    summary = generate_project_summary(
        parts, cfg, call_llm=call_llm, recorder=recorder, video_summaries=video_summaries
    )
    return ProjectSummary(
        summary=summary, parts=parts, keywords=keywords, video_summaries=video_summaries
    )


def summary_to_dict(ps: ProjectSummary) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for p in ps.parts:
        entry: dict[str, Any] = {
            "stem": p.stem,
            "lines": [p.lines[0], p.lines[1]],
            "summary": p.summary,
        }
        if p.start is not None:
            entry["start"] = p.start
        if p.end is not None:
            entry["end"] = p.end
        if p.silence is not None:
            entry["silence"] = p.silence
        parts.append(entry)
    return {
        "summary": ps.summary,
        "parts": parts,
        "keywords": ps.keywords,
        "video_summaries": ps.video_summaries,
    }


def _coerce_pair(value: Any) -> tuple[int, int] | None:
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
    parts: list[PartSummary] = []
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
            silence = raw.get("silence")
            silence = float(silence) if isinstance(silence, (int, float)) else None
            parts.append(
                PartSummary(
                    stem=stem, lines=lines, summary=s, start=start, end=end, silence=silence
                )
            )
    keywords: dict[str, list[str]] = {}
    raw_kw = data.get("keywords")
    if isinstance(raw_kw, dict):
        for k, v in raw_kw.items():
            if isinstance(k, str):
                kws = _coerce_keywords(v)
                if kws:
                    keywords[k] = kws
    video_summaries: dict[str, str] = {}
    raw_vs = data.get("video_summaries")
    if isinstance(raw_vs, dict):
        for k, v in raw_vs.items():
            if isinstance(k, str) and k and isinstance(v, str) and v.strip():
                video_summaries[k] = v.strip()
    return ProjectSummary(
        summary=summary, parts=parts, keywords=keywords, video_summaries=video_summaries
    )
