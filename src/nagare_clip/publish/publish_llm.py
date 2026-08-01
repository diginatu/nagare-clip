"""publish stage: title candidates, description lead, chapter titles, thumbnail copy.

ONE LLM call produces every piece of copy the stage owes a human: several
title candidates (hook quality varies a lot between attempts and picking from
a list is cheap — committing to a single generated title is not), a short lead
for the description, a short title per already-computed chapter, and several
alternative thumbnail copy SETS.

Timing is deliberately not the LLM's business: chapters are anchored and
merged in :mod:`nagare_clip.publish.chapters` before this call runs, and the
LLM only names them (by 1-based chapter position, the same index-mapped
contract the plan stage uses).

A thumbnail copy set is one to three lines, each carrying the role it plays
(``tag`` / ``hook`` / ``subtitle``), so a project-level compositing script can
lay out what the copy actually is instead of padding it to fill a fixed
three-line template.

Graceful by design: a hard parse failure retries, and all attempts failing
degrades to empty copy — the chapters (computed without the LLM) are still
written.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
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
    OK_EMPTY,
    UNPARSEABLE,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.publish.chapters import Chapter, format_timestamp
from nagare_clip.summary.summarize import ProjectSummary

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, str]], dict[str, Any]], str]

THUMB_ROLES = ("tag", "hook", "subtitle")
MAX_THUMB_LINES = 3


@dataclass(frozen=True)
class ThumbLine:
    role: str  # one of THUMB_ROLES
    text: str


@dataclass(frozen=True)
class ThumbSet:
    lines: list[ThumbLine]


@dataclass
class PublishCopy:
    titles: list[str] = field(default_factory=list)
    lead: str = ""
    chapter_titles: dict[int, str] = field(default_factory=dict)  # 1-based position
    thumbnail_copy: list[ThumbSet] = field(default_factory=list)


def counts_note(title_count: int, thumbnail_sets: int) -> str:
    """The prompt addendum stating the configured counts.

    Generated rather than baked into ``publish.prompt`` so the numbers the LLM
    is asked for are always the numbers the config says.
    """
    return (
        f"Give {title_count} title candidate(s) and {thumbnail_sets} alternative "
        "thumbnail copy set(s). Each thumbnail set is 1-3 lines — use only as "
        "many as the video needs; do not pad a punchy hook out to three lines."
    )


def format_publish_context(
    project_summary: ProjectSummary,
    chapters: list[Chapter],
    *,
    directions: list[PartDirection] | None = None,
    overlays: list[str] | None = None,
    duration: float = 0.0,
) -> str:
    """The user message: everything the pipeline already knows about the cut."""
    lines: list[str] = []
    if duration > 0:
        lines.append(f"Finished video length: {format_timestamp(duration)}")
        lines.append("")
    if project_summary.summary:
        lines.append(f"Overall: {project_summary.summary}")
        lines.append("")

    directions_by_key = {(d.stem, d.lines): d.direction for d in directions or []}
    current: str | None = None
    for i, p in enumerate(project_summary.parts, start=1):
        if p.stem != current:
            current = p.stem
            vs = project_summary.video_summaries.get(p.stem, "")
            lines.append(f'Video "{p.stem}"' + (f": {vs}" if vs else ""))
        entry = f"{i}: [{p.lines[0]}-{p.lines[1]}] {p.summary}"
        direction = directions_by_key.get((p.stem, p.lines))
        if direction:
            entry += f" — plan: {direction}"
        lines.append(entry)
    if project_summary.parts:
        lines.append("")

    if overlays:
        lines.append("On-screen captions the edit already places (hook material):")
        lines += [f"- {text}" for text in overlays]
        lines.append("")

    if chapters:
        lines.append("Chapters (timestamps are FINAL — give a short title for each, by number):")
        lines += [
            f"{i}: {format_timestamp(c.start)} — {c.title}" for i, c in enumerate(chapters, start=1)
        ]
    return "\n".join(lines).rstrip("\n")


def _strip_fence(response: str) -> str:
    text = response.strip()
    fence = _FENCE_RE.match(text)
    return fence.group(1) if fence else text


def _clean_str(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _parse_thumb_set(raw: Any, drop) -> ThumbSet | None:
    lines_raw = raw.get("lines") if isinstance(raw, dict) else None
    if not isinstance(lines_raw, list):
        drop("thumbnail set dropped, no 'lines' array")
        return None
    out: list[ThumbLine] = []
    for item in lines_raw:
        if not isinstance(item, dict):
            continue
        text = _clean_str(item.get("text"))
        if not text:
            continue
        role = _clean_str(item.get("role")).lower()
        if role not in THUMB_ROLES:
            # A label is not worth losing copy over: an unknown/absent role
            # reads as the line doing the hook's job.
            role = "hook"
        out.append(ThumbLine(role=role, text=text))
    if not out:
        drop("thumbnail set dropped, no usable lines")
        return None
    if len(out) > MAX_THUMB_LINES:
        drop(f"thumbnail set truncated from {len(out)} to {MAX_THUMB_LINES} line(s)")
        out = out[:MAX_THUMB_LINES]
    return ThumbSet(lines=out)


def try_parse_publish_response(
    response: str, num_chapters: int, drops: list[str] | None = None
) -> PublishCopy | None:
    """Parse the publish JSON; ``None`` on a hard failure so the caller retries.

    A hard failure is invalid JSON or no ``titles`` array — the titles are the
    one deliverable with no deterministic fallback. Everything else degrades
    field by field, with malformed entries dropped (logged).
    """

    def _drop(msg: str) -> None:
        logger.warning("publish: %s", msg)
        if drops is not None:
            drops.append(msg)

    try:
        data = json.loads(_strip_fence(response))
    except (ValueError, TypeError):
        logger.warning("publish: response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("titles"), list):
        logger.warning("publish: response has no 'titles' array; ignoring")
        return None

    titles = [t for t in (_clean_str(x) for x in data["titles"]) if t]
    if len(titles) != len(data["titles"]):
        _drop(f"{len(data['titles']) - len(titles)} empty/non-string title(s) dropped")

    chapter_titles: dict[int, str] = {}
    raw_chapters = data.get("chapters")
    if isinstance(raw_chapters, list):
        for raw in raw_chapters:
            if not isinstance(raw, dict):
                continue
            idx = raw.get("index")
            if isinstance(idx, bool) or not isinstance(idx, int):
                _drop(f"chapter title dropped, bad index {idx!r}")
                continue
            if not (1 <= idx <= num_chapters):
                _drop(f"chapter title dropped, index {idx!r} out of range")
                continue
            title = _clean_str(raw.get("title"))
            if not title:
                _drop(f"chapter title dropped, empty text at index {idx}")
                continue
            chapter_titles[idx] = title
    elif raw_chapters is not None:
        _drop("'chapters' is not an array; ignored")

    thumbnail_copy: list[ThumbSet] = []
    raw_thumbs = data.get("thumbnail_copy")
    if isinstance(raw_thumbs, list):
        for raw in raw_thumbs:
            parsed = _parse_thumb_set(raw, _drop)
            if parsed is not None:
                thumbnail_copy.append(parsed)
    elif raw_thumbs is not None:
        _drop("'thumbnail_copy' is not an array; ignored")

    lead = data.get("lead")
    if lead is not None and not isinstance(lead, str):
        _drop("'lead' is not a string; ignored")
        lead = None

    return PublishCopy(
        titles=titles,
        lead=(lead or "").strip(),
        chapter_titles=chapter_titles,
        thumbnail_copy=thumbnail_copy,
    )


def generate_publish_copy(
    project_summary: ProjectSummary,
    chapters: list[Chapter],
    cfg: dict[str, Any],
    *,
    directions: list[PartDirection] | None = None,
    overlays: list[str] | None = None,
    duration: float = 0.0,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "publish",
) -> PublishCopy:
    """Run the publish LLM once; empty copy when every attempt fails."""
    system = cfg.get("prompt", "")
    note = counts_note(int(cfg.get("title_count", 5) or 0), int(cfg.get("thumbnail_sets", 3) or 0))
    messages = [
        {"role": "system", "content": f"{system}\n\n{note}" if system else note},
        {
            "role": "user",
            "content": format_publish_context(
                project_summary,
                chapters,
                directions=directions,
                overlays=overlays,
                duration=duration,
            ),
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
        copy = try_parse_publish_response(response, num_chapters=len(chapters), drops=drops)
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
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)
        elif not copy.titles:
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
        return copy
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("publish: all %d attempt(s) failed; no copy", attempts)
    return PublishCopy()
