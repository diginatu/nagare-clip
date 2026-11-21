"""Chapter timestamps, shaped so YouTube will actually render them.

YouTube turns a timestamp list into a segmented progress bar only when all of
these hold:

- the first timestamp is exactly ``0:00``
- there are at least three of them
- they are in ascending order
- each chapter runs at least 10 seconds

The first is made to hold here (``chapter_timestamps`` renders the first entry
as ``0:00``); the others are **validated, never merged**: the publish tools'
``resolve_chapters`` sorts the chapters by finished time and reports a short or
duplicate chapter as a problem, so the agent re-chooses its lines.

The list is written either way.  YouTube auto-links timestamps in a description
regardless of these rules, so a list of two still lets a viewer jump; it simply
does not draw the segmented bar.  ``chapter_issues`` reports what is missing so
a human can see it, never suppresses the output.

Pure: seconds in, chapters out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

MIN_CHAPTER_SECONDS = 10.0
MIN_CHAPTER_COUNT = 3


@dataclass(frozen=True)
class Chapter:
    time: float  # finished-timeline seconds
    title: str


def format_timestamp(seconds: float) -> str:
    """``M:SS`` (``H:MM:SS`` past an hour), truncated toward the chapter start.

    Truncating matters: rounding 9.9s up to ``0:10`` would point a viewer past
    the moment the chapter actually begins.
    """
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _spans(times: Sequence[float], total: float) -> list[float]:
    """Rendered length of each chapter.

    The first chapter is measured from 0.0, not from its own timestamp: it is
    rendered as ``0:00``, so the span a viewer sees starts at the beginning of
    the video however late the part's surviving footage begins.
    """
    if not times:
        return []
    starts = [0.0, *times[1:]]
    ends = [*times[1:], max(total, times[-1])]
    return [end - start for start, end in zip(starts, ends)]


def chapter_timestamps(chapters: Sequence[Chapter]) -> list[str]:
    """Rendered timestamps, with the first entry forced to ``0:00``.

    The first part almost never starts at zero once its opening has been cut,
    and YouTube wants the list anchored at the very start of the video.
    """
    return [
        "0:00" if i == 0 else format_timestamp(chapter.time) for i, chapter in enumerate(chapters)
    ]


def render_chapter_lines(chapters: Sequence[Chapter]) -> list[str]:
    """``M:SS Title``, one per line — the format YouTube parses."""
    return [
        f"{stamp} {chapter.title}" for stamp, chapter in zip(chapter_timestamps(chapters), chapters)
    ]


def chapter_issues(
    chapters: Sequence[Chapter],
    total: float,
    min_duration: float = MIN_CHAPTER_SECONDS,
    min_count: int = MIN_CHAPTER_COUNT,
) -> list[str]:
    """What still stops YouTube rendering these as chapters (``[]`` = nothing).

    The leading ``0:00`` is guaranteed by ``chapter_timestamps`` and the
    order by the tools' sort, so what can remain is a list too short to
    qualify, or a chapter under the minimum (which ``write`` refuses before
    it gets here).
    """
    issues: list[str] = []
    if len(chapters) < min_count:
        issues.append(f"only {len(chapters)} chapter(s); YouTube needs at least {min_count}")
    spans = _spans([c.time for c in chapters], total)
    short = [
        f"{chapters[i].title!r} runs {span:.1f}s"
        for i, span in enumerate(spans)
        if span < min_duration
    ]
    if short:
        issues.append(f"chapter(s) shorter than {min_duration:.0f}s: " + ", ".join(short))
    return issues
