"""Build publish.json from a validated edit (pure, no LLM)."""

from __future__ import annotations

from typing import Any

from nagare_clip.publish.chapters import (
    MIN_CHAPTER_SECONDS,
    Chapter,
    chapter_issues,
    chapter_timestamps,
)
from nagare_clip.publish.timeline import Placement, first_surviving_time, total_duration
from nagare_clip.publish.tools.edit import PublishEdit, ResolvedChapter, assemble_description


def _thumbnail_copy(sets: list[dict]) -> list[dict]:
    """The edit's sets, each line's absent role filled with ``hook``."""
    out = []
    for s in sets:
        lines = [{"role": "hook", **line} for line in s.get("lines", [])]
        out.append({**s, "lines": lines})
    return out


def _stills(stills: list[dict], placements: list[Placement] | None) -> list[dict[str, Any]]:
    out = []
    for still in stills:
        t = float(still["source_time"])
        timeline = (
            None
            if placements is None
            else first_surviving_time(placements, still["stem"], t, t + 0.001)
        )
        out.append(
            {
                "stem": still["stem"],
                "source_time": round(t, 3),
                "timeline_time": round(timeline, 3) if timeline is not None else None,
                "kind": still["kind"],
                "label": still["label"],
                "line": still.get("line"),
                "path": still["path"],
            }
        )
    return out


def build_publish(
    edit: PublishEdit,
    chapters: list[ResolvedChapter],
    placements: list[Placement] | None,
    stills: list[dict],
    *,
    min_chapter: float = MIN_CHAPTER_SECONDS,
) -> dict[str, Any]:
    """publish.json in today's shape.

    Chapters are already validated (none short or duplicated), so they are not
    merged again; the first is shown as ``0:00`` by ``chapter_timestamps``.
    """
    chs = [Chapter(c.time, c.title) for c in chapters]
    total = total_duration(placements) if placements else 0.0
    issues = chapter_issues(chs, total, min_duration=min_chapter)
    return {
        "titles": list(edit.titles),
        "lead": edit.lead,
        "description": assemble_description(edit.lead, chs),
        "chapters": [
            {"time": round(c.time, 3), "timestamp": stamp, "title": c.title}
            for c, stamp in zip(chs, chapter_timestamps(chs))
        ],
        "chapters_qualify": not issues,
        "chapter_issues": issues,
        "thumbnail_copy": _thumbnail_copy(edit.thumbnails),
        "thumbnails": _stills(stills, placements),
    }
