"""Chapter timestamps, shaped so YouTube will actually render them.

YouTube turns a timestamp list into a segmented progress bar only when all of
these hold:

- the first timestamp is exactly ``0:00``
- there are at least three of them
- they are in ascending order
- each chapter runs at least 10 seconds

The mapping from summary parts to finished-timeline seconds rarely satisfies
them on its own — the first part's opening is usually cut, so it starts late,
and a part compressed inside a timelapse can be a couple of seconds long — so
this module *makes* them hold: the first entry is rendered as ``0:00`` and any
chapter whose span falls short is merged into a neighbour.

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


def build_chapters(
    entries: Sequence[tuple[float, str]],
    total: float,
    min_duration: float = MIN_CHAPTER_SECONDS,
) -> list[Chapter]:
    """Enforce ascending order and the minimum chapter length.

    Entries must already be in intended reading order (parts in transcript
    order, videos in timeline order); an entry that does not advance past the
    previous one is dropped rather than reordered, since a title out of order
    would be wrong wherever it landed.

    A chapter shorter than *min_duration* gives way to a neighbour: it is
    dropped and the previous chapter's title stretches over it.  The first
    chapter has no previous neighbour, so it gives way to the next one instead,
    which then inherits ``0:00`` when the list is rendered.  Dropping only ever
    lengthens the surviving chapters, so the sweep repeats until nothing is
    short — or until a single chapter is left, which is as far as merging can
    go.
    """
    kept: list[Chapter] = []
    for time, title in entries:
        if kept and time <= kept[-1].time:
            continue
        kept.append(Chapter(time=float(time), title=title))

    while len(kept) > 1:
        spans = _spans([c.time for c in kept], total)
        short = next((i for i, span in enumerate(spans) if span < min_duration), None)
        if short is None:
            break
        kept.pop(short)
    return kept


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

    Ascending order and the leading ``0:00`` are guaranteed by
    ``build_chapters``/``render_chapter_lines``, so what can survive is a list
    too short to qualify, or — when merging bottomed out at one chapter — a
    video too short for even that one to clear the minimum.
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
        issues.append(
            f"chapter(s) shorter than {min_duration:.0f}s and unmergeable: " + ", ".join(short)
        )
    return issues
