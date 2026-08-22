"""plan stage: coarse, cross-video rough directions per part.

Runs once project-wide after ``summary``.  Reads the per-part summaries (with
line ranges) of all videos and asks a larger LLM for a rough editorial direction
per part (remove / shorten / speed / feature …), decided with the whole project
in view.  The result is written to ``plan.json`` for human review and consumed by
the per-video ``director`` stage.

It is a **pure function of the summaries**: same ``summary.json`` in, same
directions out.  Revising a plan against what the human said is a separate job
with an opposite shape (name what changes, leave the rest alone) and lives in
the ``plan_revise`` stage — putting both in one prompt puts instructions about
deleting existing directions in front of a first run that has none.

Graceful by design: any LLM/parse failure degrades to an empty direction list.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nagare_clip.director.director_llm import _FENCE_RE
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
from nagare_clip.summary.summarize import PartSummary, ProjectSummary
from nagare_clip.text_filter.llm_filter import _call_llm
from nagare_clip.timing import format_dur_gap

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, str]], dict[str, Any]], str]


@dataclass
class PartDirection:
    stem: str
    lines: tuple[int, int]  # 1-based inclusive (start, end)
    direction: str


def assign_to_parts(
    parts: list[PartSummary], directions: list[PartDirection]
) -> dict[int, list[PartDirection]]:
    """Group directions under the part each belongs to.

    A direction may cover only part of its part (a split), so it is matched by
    largest line overlap rather than by an exact range — a hand-edited range that
    straddles a boundary still lands somewhere readable instead of vanishing.
    A direction overlapping no part of its own video is dropped.
    """
    grouped: dict[int, list[PartDirection]] = {}
    for d in directions:
        best_idx, best_overlap = -1, 0
        for i, part in enumerate(parts):
            if part.stem != d.stem:
                continue
            overlap = min(part.lines[1], d.lines[1]) - max(part.lines[0], d.lines[0]) + 1
            if overlap > best_overlap:
                best_idx, best_overlap = i, overlap
        if best_idx < 0:
            logger.warning("plan: direction %s %s matches no part", d.stem, d.lines)
            continue
        grouped.setdefault(best_idx, []).append(d)
    return grouped


def format_parts_for_plan(
    project_summary: ProjectSummary,
    directions: list[PartDirection] | None = None,
    ids: list[str] | None = None,
) -> str:
    """The parts document both plan and plan_revise show the model.

    With no *directions* this is exactly what ``plan`` sends.  ``plan_revise``
    passes the existing directions with their (abbreviated) ids, which render
    under the part each belongs to — the only place an id is ever shown.
    """
    lines: list[str] = []
    if project_summary.summary:
        lines.append(f"Overall: {project_summary.summary}")
        lines.append("")
    parts = project_summary.parts
    video_summaries = project_summary.video_summaries
    by_part = assign_to_parts(parts, directions or [])
    id_of = {
        id(d): (ids[i] if ids and i < len(ids) else "") for i, d in enumerate(directions or [])
    }
    current: str | None = None
    for i, p in enumerate(parts):
        if p.stem != current:
            current = p.stem
            vs = video_summaries.get(p.stem, "")
            if vs:
                lines.append(f'Video "{p.stem}": {vs}')
        raw = p.end - p.start if p.start is not None and p.end is not None else None
        sil = p.silence if isinstance(p.silence, (int, float)) and p.silence > 0 else None
        dur = max(raw - sil, 0.0) if raw is not None and sil else raw
        gap: float | None = None
        if i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt.stem == p.stem and p.end is not None and nxt.start is not None:
                gap = nxt.start - p.end
        bracket = format_dur_gap(dur, gap, sil)
        prefix = f"{i + 1}: {p.stem} [{p.lines[0]}-{p.lines[1]}]"
        head = f"{prefix} {bracket}" if bracket else prefix
        lines.append(f"{head} — {p.summary}")
        for d in sorted(by_part.get(i, []), key=lambda d: d.lines):
            tag = f"[{id_of.get(id(d), '')}] " if id_of.get(id(d)) else ""
            lines.append(f"    {tag}lines {d.lines[0]}-{d.lines[1]}: {d.direction}")
    return "\n".join(lines)


def coerce_lines(value: Any, part: PartSummary) -> tuple[int, int] | None:
    """A direction's own line range, validated against its part's range.

    ``None``/absent means "the whole part" (today's behaviour).  A range must be
    a 2-integer pair inside the part it belongs to — a direction is an
    instruction about one part's footage, so a range reaching outside it names
    lines the part does not cover and is dropped rather than clamped.
    """
    pair = _coerce_pair(value)
    if pair is None:
        return None
    a, b = pair
    if a > b or a < part.lines[0] or b > part.lines[1]:
        return None
    return (a, b)


def try_parse_plan_response(
    response: str, parts: list[PartSummary], drops: list[str] | None = None
) -> list[PartDirection] | None:
    """Parse ``{"directions": [{"index": N, "lines"?: [a, b], "direction": "…"}]}``.

    Returns ``None`` on a hard parse failure (invalid JSON / no ``directions``
    array) so the caller can retry; otherwise a (possibly empty) direction list
    with malformed/out-of-range entries dropped (logged).

    An entry's optional ``lines`` narrows the direction to part of its part, so
    one summary part can yield several directions (a mixed part split into its
    threads).  Directions come back ordered by part, then by line.
    """

    def _drop(msg: str) -> None:
        logger.warning("plan: %s", msg)
        if drops is not None:
            drops.append(msg)

    text = response.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("plan: response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("directions"), list):
        logger.warning("plan: response has no 'directions' array; ignoring")
        return None

    # Keyed by (part index, line range) so a re-stated range replaces the earlier
    # one while a genuinely different range splits the part.
    out: dict[tuple[int, tuple[int, int]], str] = {}
    for raw in data["directions"]:
        if not isinstance(raw, dict):
            continue
        idx = raw.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            _drop(f"direction dropped, bad index {idx!r}")
            continue
        if not (1 <= idx <= len(parts)):
            _drop(f"direction dropped, index {idx!r} out of range")
            continue
        part = parts[idx - 1]
        direction = raw.get("direction")
        if not isinstance(direction, str) or direction == "":
            _drop("direction dropped, empty/missing text")
            continue
        lines = part.lines
        if raw.get("lines") is not None:
            narrowed = coerce_lines(raw.get("lines"), part)
            if narrowed is None:
                _drop(
                    f"direction dropped, lines {raw.get('lines')!r} outside "
                    f"part {idx} range {part.lines[0]}-{part.lines[1]}"
                )
                continue
            lines = narrowed
        out[(idx, lines)] = direction

    return [
        PartDirection(stem=parts[idx - 1].stem, lines=lines, direction=direction)
        for (idx, lines), direction in sorted(out.items())
    ]


def generate_plan(
    project_summary: ProjectSummary,
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "plan",
) -> list[PartDirection]:
    """Run the plan LLM and return one rough direction per part."""
    parts = project_summary.parts
    if not parts:
        return []
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": format_parts_for_plan(project_summary)},
    ]
    recorder.begin(unit)
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "plan: LLM call failed (attempt %d/%d)",
                attempt + 1,
                attempts,
                exc_info=True,
            )
            recorder.attempt(
                unit=unit,
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
        parsed = try_parse_plan_response(response, parts, drops)
        if parsed is None:
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=messages,
                response=response,
                outcome=UNPARSEABLE,
                reason="invalid JSON / no 'directions' array",
                cfg=attempt_cfg,
            )
            logger.warning("plan: response unparseable (attempt %d/%d)", attempt + 1, attempts)
            continue
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)
        elif not parsed:
            outcome, reason = OK_EMPTY, ""
        else:
            outcome, reason = OK, ""
        recorder.attempt(
            unit=unit,
            attempt=attempt,
            total=attempts,
            messages=messages,
            response=response,
            outcome=outcome,
            reason=reason,
            cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=outcome, reason=reason)
        return parsed
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("plan: all %d attempt(s) failed; no directions", attempts)
    return []


def plan_to_dict(directions: list[PartDirection]) -> dict[str, Any]:
    return {
        "directions": [
            {
                "stem": d.stem,
                "lines": [d.lines[0], d.lines[1]],
                "direction": d.direction,
            }
            for d in directions
        ]
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


def plan_from_dict(data: Any) -> list[PartDirection]:
    if not isinstance(data, dict):
        return []
    raw_directions = data.get("directions")
    if not isinstance(raw_directions, list):
        return []
    out: list[PartDirection] = []
    for raw in raw_directions:
        if not isinstance(raw, dict):
            continue
        stem = raw.get("stem")
        lines = _coerce_pair(raw.get("lines"))
        direction = raw.get("direction")
        if not isinstance(stem, str) or lines is None or not isinstance(direction, str):
            continue
        out.append(PartDirection(stem=stem, lines=lines, direction=direction))
    return out
