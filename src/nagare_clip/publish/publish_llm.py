"""One LLM call for everything a human types by hand after the rough cut:
title candidates, a description lead, chapter titles and thumbnail copy.

Deliberately several candidates, not one answer.  Hook quality varies a lot
between attempts and picking from a list is cheap, whereas committing to a
single generated title is not — the same reasoning applies to the thumbnail
copy, which comes back as alternative *sets* of one to three lines, each line
tagged with the role it plays so a layout can follow the copy instead of the
copy being padded to fill a template.

**This call is blind, on purpose.**  It emits ``role`` and ``text`` and
nothing else: no colours, no placement, no font, and it is shown no frames.
The look is decided afterwards by ``publish/pairing.py``, which has the frame
descriptions.  Hooks come from the story, not from the pictures that happen to
be on hand — and a style key arriving in this response is something the model
was not asked for, so it is dropped at the parse boundary rather than carried
into ``publish.json``.

Chapter *timestamps* are never asked for: they come from the finished timeline
(``publish.timeline``), which the LLM cannot see.  It supplies only the titles,
keyed by part index, and any part it skips falls back to that part's summary.

Graceful by design: an LLM/parse failure degrades to empty copy, and every
malformed item inside a valid response is dropped individually.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from nagare_clip.director.director_llm import _FENCE_RE
from nagare_clip.llm_client import call_llm as _call_llm
from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    UNPARSEABLE,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.render.thumbnail import (
    MAX_THUMB_LINES,
    VALID_ROLES,
    ThumbLine,
    ThumbSet,
)
from nagare_clip.summary.summarize import ProjectSummary

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, str]], dict[str, Any]], str]

__all__ = [
    "MAX_THUMB_LINES",
    "VALID_ROLES",
    "PublishCopy",
    "ThumbLine",
    "ThumbSet",
    "format_publish_context",
    "generate_publish_copy",
    "thumbnail_copy_to_dict",
    "try_parse_publish_response",
]


@dataclass
class PublishCopy:
    titles: list[str] = field(default_factory=list)
    lead: str = ""
    chapter_titles: dict[int, str] = field(default_factory=dict)  # 1-based part index -> title
    thumbnail_copy: list[ThumbSet] = field(default_factory=list)


def format_publish_context(
    project_summary: ProjectSummary,
    directions: Sequence[PartDirection] | None = None,
    overlay_texts: Sequence[str] | None = None,
) -> str:
    """The whole project as one document: summaries, parts, plan, captions.

    Parts are numbered globally 1-based — the same numbering the response's
    ``chapters[].index`` refers to.
    """
    by_part = {(d.stem, d.lines): d.direction for d in directions or []}
    lines: list[str] = []
    if project_summary.summary:
        lines.append(f"Overall: {project_summary.summary}")
        lines.append("")
    current: str | None = None
    for i, part in enumerate(project_summary.parts):
        if part.stem != current:
            current = part.stem
            video_summary = project_summary.video_summaries.get(part.stem, "")
            lines.append(
                f'Video "{part.stem}": {video_summary}' if video_summary else f'Video "{part.stem}"'
            )
        head = f"{i + 1}: {part.stem} [{part.lines[0]}-{part.lines[1]}] — {part.summary}"
        direction = by_part.get((part.stem, part.lines))
        lines.append(f"{head} (plan: {direction})" if direction else head)
    if overlay_texts:
        lines.append("")
        lines.append("On-screen captions the edit places at its payoff moments:")
        lines += [f"- {text}" for text in overlay_texts]
    return "\n".join(lines)


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_titles(value: Any) -> list[str]:
    out: list[str] = []
    for raw in value if isinstance(value, list) else []:
        title = _clean_str(raw)
        if title and title not in out:
            out.append(title)
    return out


def _parse_chapter_titles(
    value: Any, num_parts: int, drop: Callable[[str], None]
) -> dict[int, str]:
    out: dict[int, str] = {}
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not (1 <= index <= num_parts):
            drop(f"chapter title dropped, bad index {index!r}")
            continue
        title = _clean_str(raw.get("title"))
        if not title:
            drop(f"chapter title dropped, empty title for part {index}")
            continue
        out[index] = title
    return out


def _parse_thumb_set(raw: Any, drop: Callable[[str], None]) -> ThumbSet:
    raw_lines = raw.get("lines") if isinstance(raw, dict) else None
    lines: list[ThumbLine] = []
    for entry in raw_lines if isinstance(raw_lines, list) else []:
        if not isinstance(entry, dict):
            continue
        role = _clean_str(entry.get("role")).lower()
        text = _clean_str(entry.get("text"))
        if role not in VALID_ROLES:
            drop(f"thumbnail line dropped, unknown role {entry.get('role')!r}")
            continue
        if not text:
            drop(f"thumbnail line dropped, empty text ({role})")
            continue
        # Style deliberately empty: this call does not choose the look.
        lines.append(ThumbLine(role=role, text=text))
    if len(lines) > MAX_THUMB_LINES:
        drop(f"thumbnail set trimmed from {len(lines)} to {MAX_THUMB_LINES} line(s)")
        lines = lines[:MAX_THUMB_LINES]
    return ThumbSet(lines=lines)


def try_parse_publish_response(
    response: str, num_parts: int, drops: list[str] | None = None
) -> PublishCopy | None:
    """Parse the publish response; ``None`` on a hard failure (retryable).

    Hard failure is invalid JSON, a non-object, or a missing/empty ``titles``
    array — several title candidates are the point of the call, so an attempt
    that forgot them is worth retrying rather than degrading.  Everything else
    is lenient: a malformed chapter title or thumbnail line is dropped on its
    own and logged.
    """

    def drop(msg: str) -> None:
        logger.warning("publish: %s", msg)
        if drops is not None:
            drops.append(msg)

    text = response.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("publish: response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict):
        logger.warning("publish: response is not a JSON object; ignoring")
        return None
    titles = _parse_titles(data.get("titles"))
    if not titles:
        logger.warning("publish: response has no usable 'titles'; ignoring")
        return None

    raw_sets = data.get("thumbnail_copy")
    thumbnail_copy: list[ThumbSet] = []
    for raw in raw_sets if isinstance(raw_sets, list) else []:
        thumb_set = _parse_thumb_set(raw, drop)
        if thumb_set.lines:
            thumbnail_copy.append(thumb_set)

    return PublishCopy(
        titles=titles,
        lead=_clean_str(data.get("lead")),
        chapter_titles=_parse_chapter_titles(data.get("chapters"), num_parts, drop),
        thumbnail_copy=thumbnail_copy,
    )


def generate_publish_copy(
    project_summary: ProjectSummary,
    cfg: dict[str, Any],
    *,
    directions: Sequence[PartDirection] | None = None,
    overlay_texts: Sequence[str] | None = None,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "publish",
) -> PublishCopy:
    """Run the publish LLM once (with retries); empty copy when it never works."""
    parts = project_summary.parts
    if not parts:
        logger.warning("publish: no summary parts; nothing to write copy from")
        return PublishCopy()
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {
            "role": "user",
            "content": format_publish_context(project_summary, directions, overlay_texts),
        },
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
                "publish: LLM call failed (attempt %d/%d)", attempt + 1, attempts, exc_info=True
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
        copy = try_parse_publish_response(response, num_parts=len(parts), drops=drops)
        if copy is None:
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=messages,
                response=response,
                outcome=UNPARSEABLE,
                reason="invalid JSON / no 'titles' array",
                cfg=attempt_cfg,
            )
            logger.warning("publish: response unparseable (attempt %d/%d)", attempt + 1, attempts)
            continue
        outcome, reason = (
            (DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)) if drops else (OK, "")
        )
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
        return copy
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("publish: all %d attempt(s) failed; no copy", attempts)
    return PublishCopy()


def thumbnail_copy_to_dict(copy: PublishCopy) -> list[dict[str, Any]]:
    """The thumbnail sets as plain JSON: copy, background and look together.

    The line count is never normalised -- a punchier video may want only a
    hook.  Style keys sit beside the text they apply to, and each set names the
    picture it goes on, which is what makes publish.json hand-editable: change
    a colour or a background, re-run the render stage.

    ``background`` is written even when empty, so a human opening the file can
    see where a path goes without reading the docs first.
    """
    return [
        {
            "lines": [
                {"role": line.role, "text": line.text, **line.style} for line in thumb_set.lines
            ],
            "background": thumb_set.background,
            **thumb_set.style,
        }
        for thumb_set in copy.thumbnail_copy
    ]
