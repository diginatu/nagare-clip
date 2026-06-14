"""plan stage: coarse, cross-video rough directions per part.

Runs once project-wide after ``summary``.  Reads the per-part summaries (with
line ranges) of all videos and asks a larger LLM for a rough editorial direction
per part (remove / shorten / speed / keep …), decided with the whole project in
view.  The result is written to ``plan.json`` for human review and consumed by
the per-video ``director`` stage.

Graceful by design: any LLM/parse failure degrades to an empty direction list.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from nagare_clip.director.director_llm import _FENCE_RE
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.stage2.llm_filter import _call_llm
from nagare_clip.summary.summarize import ProjectSummary

logger = logging.getLogger(__name__)

CallLLM = Callable[[List[Dict[str, str]], Dict[str, Any]], str]


@dataclass
class PartDirection:
    stem: str
    lines: Tuple[int, int]  # 1-based inclusive (start, end)
    direction: str


def _format_parts_for_plan(project_summary: ProjectSummary) -> str:
    lines: List[str] = []
    if project_summary.summary:
        lines.append(f"Overall: {project_summary.summary}")
        lines.append("")
    for i, p in enumerate(project_summary.parts):
        lines.append(f"{i + 1}: {p.stem} [{p.lines[0]}-{p.lines[1]}] — {p.summary}")
    return "\n".join(lines)


def try_parse_plan_response(
    response: str, num_parts: int
) -> Optional[Dict[int, str]]:
    """Parse ``{"directions": [{"index": N, "direction": "…"}]}``.

    Returns ``None`` on a hard parse failure (invalid JSON / no ``directions``
    array) so the caller can retry; otherwise a (possibly empty) ``{index:
    direction}`` map with malformed/out-of-range entries dropped (logged).
    """
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

    out: Dict[int, str] = {}
    for raw in data["directions"]:
        if not isinstance(raw, dict):
            continue
        idx = raw.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            logger.warning("plan: direction dropped, bad index %r", idx)
            continue
        if not (1 <= idx <= num_parts):
            logger.warning("plan: direction dropped, index %r out of range", idx)
            continue
        direction = raw.get("direction")
        if not isinstance(direction, str) or direction == "":
            logger.warning("plan: direction dropped, empty/missing text")
            continue
        out[idx] = direction
    return out


def generate_plan(
    project_summary: ProjectSummary,
    cfg: Dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
) -> List[PartDirection]:
    """Run the plan LLM and return one rough direction per part (where given)."""
    parts = project_summary.parts
    if not parts:
        return []
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": _format_parts_for_plan(project_summary)},
    ]
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        try:
            response = call_llm(messages, cfg_for_attempt(cfg, attempt))
        except Exception:
            logger.warning(
                "plan: LLM call failed (attempt %d/%d)",
                attempt + 1,
                attempts,
                exc_info=True,
            )
            continue
        mapping = try_parse_plan_response(response, num_parts=len(parts))
        if mapping is not None:
            return [
                PartDirection(stem=p.stem, lines=p.lines, direction=mapping[i + 1])
                for i, p in enumerate(parts)
                if (i + 1) in mapping
            ]
        logger.warning(
            "plan: response unparseable (attempt %d/%d)", attempt + 1, attempts
        )
    logger.warning("plan: all %d attempt(s) failed; no directions", attempts)
    return []


def plan_to_dict(directions: List[PartDirection]) -> Dict[str, Any]:
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


def _coerce_pair(value: Any) -> Optional[Tuple[int, int]]:
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        return None
    a, b = value
    if isinstance(a, bool) or isinstance(b, bool):
        return None
    if not isinstance(a, int) or not isinstance(b, int):
        return None
    return (a, b)


def plan_from_dict(data: Any) -> List[PartDirection]:
    if not isinstance(data, dict):
        return []
    raw_directions = data.get("directions")
    if not isinstance(raw_directions, list):
        return []
    out: List[PartDirection] = []
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
