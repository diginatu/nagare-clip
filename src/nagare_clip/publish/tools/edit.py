"""Parse and validate a publish edit: every problem at once (pure, no LLM).

The edit is ``{"titles", "lead", "chapters": [{"line", "title"}], "thumbnails"}``
in the director's display numbering.  Seconds are never in the file; the tool
computes every chapter time from the finished timeline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nagare_clip.director.tools.project import Project
from nagare_clip.publish.chapters import Chapter, _spans, render_chapter_lines
from nagare_clip.publish.timeline import Placement, total_duration
from nagare_clip.publish.tools.lines import line_span, line_time
from nagare_clip.render.thumbnail import line_style_problems, set_style_problems

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MIN_CHAPTER_COUNT = 3
TOP_KEYS = ("titles", "lead", "chapters", "thumbnails")
CHAPTER_KEYS = ("line", "title")


@dataclass(frozen=True)
class ChapterRef:
    line: int
    title: str


@dataclass(frozen=True)
class ResolvedChapter:
    line: int
    time: float
    title: str


@dataclass(frozen=True)
class PublishEdit:
    titles: list[str]
    lead: str
    chapters: list[ChapterRef]
    thumbnails: list[dict]


def assemble_description(lead: str, chapters: list[Chapter]) -> str:
    """The lead, a blank line, then the chapter lines (empty parts dropped)."""
    return "\n\n".join(part for part in (lead, "\n".join(render_chapter_lines(chapters))) if part)


def _titles(raw: Any, problems: list[str]) -> list[str]:
    if not isinstance(raw, list) or not raw:
        problems.append("titles: want a non-empty list of strings")
        return []
    titles: list[str] = []
    for i, title in enumerate(raw):
        if not isinstance(title, str) or not title.strip():
            problems.append(f"titles[{i}]: want a non-empty string")
            continue
        if len(title) > MAX_TITLE_CHARS:
            problems.append(f"titles[{i}]: {len(title)} characters, over {MAX_TITLE_CHARS}")
        if "<" in title or ">" in title:
            problems.append(f"titles[{i}]: contains < or > (YouTube rejects both)")
        titles.append(title)
    return titles


def _chapter_refs(raw: Any, problems: list[str]) -> list[ChapterRef]:
    if not isinstance(raw, list):
        problems.append("chapters: want a list")
        return []
    refs: list[ChapterRef] = []
    for i, item in enumerate(raw):
        where = f"chapters[{i}]"
        if not isinstance(item, Mapping):
            problems.append(f"{where}: want an object with line and title")
            continue
        bad = False
        for key in item:
            if key not in CHAPTER_KEYS:
                problems.append(f"{where}: unknown key {key!r}")
                bad = True
        line, title = item.get("line"), item.get("title")
        if not isinstance(line, int) or isinstance(line, bool):
            problems.append(f"{where}: line must be an integer")
            bad = True
        if not isinstance(title, str) or not title.strip():
            problems.append(f"{where}: title must be a non-empty string")
            bad = True
        if not bad:
            refs.append(ChapterRef(line, title))
    return refs


def chapter_count_warning(count: int) -> str | None:
    if count >= MIN_CHAPTER_COUNT:
        return None
    return (
        f"only {count} chapter(s); YouTube links them but draws no segmented "
        f"bar below {MIN_CHAPTER_COUNT}"
    )


def resolve_chapters(
    refs: list[ChapterRef],
    project: Project,
    placements: list[Placement] | None,
    *,
    min_chapter: float,
) -> tuple[list[ResolvedChapter], list[str]]:
    """``(chapters sorted by finished time, problems)`` for display-line refs.

    The one resolution both ``write`` (through :func:`validate_edit`) and the
    ``chapters`` command use, so a preview can never disagree with the write.
    """
    problems: list[str] = []
    if not refs:
        return [], problems
    if placements is None:
        problems.append("chapters: the finished timeline is unavailable (no intervals yet)")
        return [], problems
    resolved: list[tuple[int, ResolvedChapter]] = []
    for i, ref in enumerate(refs):
        where = f"chapters[{i}] (line {ref.line})"
        if not 1 <= ref.line <= len(project.view.lines):
            problems.append(f"{where}: no such line (1-{len(project.view.lines)})")
            continue
        if line_span(project, ref.line) is None:
            problems.append(f"{where}: the line has no timing")
            continue
        time = line_time(placements, project, ref.line)
        if time is None:
            problems.append(f"{where}: the line is entirely cut from the finished video")
            continue
        resolved.append((i, ResolvedChapter(ref.line, time, ref.title)))
    resolved.sort(key=lambda pair: pair[1].time)
    for (_, a), (j, b) in zip(resolved, resolved[1:]):
        if a.time == b.time:
            problems.append(
                f"chapters[{j}] (line {b.line}): at the same time as line {a.line} ({a.time:.1f}s)"
            )
    spans = _spans([c.time for _, c in resolved], total_duration(placements))
    for (i, c), span in zip(resolved, spans):
        if span < min_chapter:
            problems.append(
                f"chapters[{i}] (line {c.line}): runs {span:.1f}s, "
                f"shorter than the minimum {min_chapter:g}s"
            )
    return [c for _, c in resolved], problems


def _thumbnails(
    raw: Any, fonts: Mapping[str, str], publish_dir: Path, problems: list[str]
) -> list[dict]:
    if not isinstance(raw, list):
        problems.append("thumbnails: want a list")
        return []
    sets: list[dict] = []
    for i, item in enumerate(raw):
        where = f"thumbnails[{i}]"
        if not isinstance(item, Mapping):
            problems.append(f"{where}: want an object")
            continue
        for p in set_style_problems(item):
            problems.append(f"{where}: {p}")
        background = item.get("background")
        if not isinstance(background, str) or not background.strip():
            problems.append(f"{where}: background is required (a path to a still)")
        else:
            path = Path(background)
            if not path.is_absolute():
                path = publish_dir / path
            if not path.is_file():
                problems.append(f"{where}: background {background!r} does not exist")
        lines = item.get("lines")
        if not isinstance(lines, list) or not lines:
            problems.append(f"{where}: lines must be a non-empty list")
        else:
            for j, line in enumerate(lines):
                lwhere = f"{where}.lines[{j}]"
                if not isinstance(line, Mapping):
                    problems.append(f"{lwhere}: want an object")
                    continue
                text = line.get("text")
                if not isinstance(text, str) or not text.strip():
                    problems.append(f"{lwhere}: text must be a non-empty string")
                for p in line_style_problems(line, fonts):
                    problems.append(f"{lwhere}: {p}")
        sets.append(dict(item))
    return sets


def validate_edit(
    data: Any,
    project: Project,
    placements: list[Placement] | None,
    *,
    fonts: Mapping[str, str],
    publish_dir: Path,
    min_chapter: float,
) -> tuple[PublishEdit | None, list[ResolvedChapter], list[str], list[str]]:
    """``(edit, chapters sorted by time, problems, warnings)``; every problem at once."""
    problems: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, Mapping):
        return None, [], ["edit: want a JSON object"], warnings
    for key in data:
        if key not in TOP_KEYS:
            problems.append(f"unknown key {key!r} (allowed: {', '.join(TOP_KEYS)})")
    titles = _titles(data.get("titles"), problems)
    lead = data.get("lead", "")
    if not isinstance(lead, str):
        problems.append("lead: want a string")
        lead = ""
    refs = _chapter_refs(data.get("chapters", []), problems)
    chapters, chapter_problems = resolve_chapters(
        refs, project, placements, min_chapter=min_chapter
    )
    problems.extend(chapter_problems)
    thumbnails = _thumbnails(data.get("thumbnails", []), fonts, publish_dir, problems)

    description = assemble_description(lead, [Chapter(c.time, c.title) for c in chapters])
    if len(description) > MAX_DESCRIPTION_CHARS:
        problems.append(f"description: {len(description)} characters, over {MAX_DESCRIPTION_CHARS}")
    warning = chapter_count_warning(len(chapters))
    if warning:
        warnings.append(warning)
    edit = PublishEdit(titles, lead, refs, thumbnails)
    return edit, chapters, problems, warnings
