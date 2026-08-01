"""Chapter list: summary parts anchored on the finished timeline (pure).

YouTube only turns a description's timestamp list into chapters when ALL of:

* the first timestamp is exactly ``0:00``
* there are at least three of them
* they are in ascending order
* each chapter runs at least 10 seconds

so this module *satisfies* those conditions rather than hoping the mapping
lands correctly: the first entry is forced to ``0:00`` (a part's opening is
routinely cut, so the first part rarely starts at zero), and any chapter that
lands under ``min_chapter`` seconds is merged into a neighbour (a part
compressed inside a timelapse reaches that easily).

The list is written even when it cannot qualify — YouTube auto-links
timestamps in a description regardless, so a list of two still lets a viewer
jump; it simply does not draw a segmented progress bar. ``chapter_issues``
reports what is missing so the human sees it, instead of the output being
suppressed behind a gate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from nagare_clip.publish.timeline import Placed, first_surviving

MIN_CHAPTER = 10.0
MIN_CHAPTER_COUNT = 3


@dataclass(frozen=True)
class Chapter:
    start: float  # finished-timeline seconds
    title: str
    stem: str
    part_index: int  # 1-based index of the summary.json part it came from


def format_timestamp(seconds: float) -> str:
    """``M:SS`` (or ``H:MM:SS`` past an hour) — YouTube's chapter format."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _anchor(parts, placed: list[Placed]) -> list[Chapter]:
    """One chapter per part that has surviving footage, in timeline order."""
    out: list[Chapter] = []
    for i, part in enumerate(parts, start=1):
        if part.start is None:
            continue
        hit = first_surviving(placed, part.stem, part.start, part.end)
        if hit is None:
            continue  # part cut in its entirety -> no chapter
        out.append(Chapter(start=hit[1], title=part.summary, stem=part.stem, part_index=i))
    out.sort(key=lambda c: c.start)
    return out


def _merge_short(chapters: list[Chapter], total: float, min_chapter: float) -> list[Chapter]:
    """Absorb every chapter shorter than *min_chapter* into a neighbour.

    A short chapter is absorbed by the one BEFORE it (which keeps its title and
    simply runs longer); the first chapter has no predecessor, so it is instead
    absorbed by the one after it, which inherits the earlier start — and since
    the first entry is forced to ``0:00`` anyway, that is the same thing.
    Repeats until stable: dropping an entry only ever lengthens its neighbour,
    so this terminates.
    """
    out = list(chapters)
    while len(out) > 1:
        for i, c in enumerate(out):
            end = out[i + 1].start if i + 1 < len(out) else total
            if end - c.start >= min_chapter:
                continue
            del out[0 if i == 0 else i]
            break
        else:
            break
    return out


def build_chapters(
    parts,
    placed: list[Placed],
    total: float,
    *,
    min_chapter: float = MIN_CHAPTER,
) -> list[Chapter]:
    """Chapters for *parts* (``summary.json`` PartSummary objects).

    Titles start out as the part summaries; ``apply_titles`` replaces them with
    the publish LLM's short forms when it produced any.
    """
    chapters = _merge_short(_anchor(parts, placed), total, min_chapter)
    if chapters:
        chapters[0] = replace(chapters[0], start=0.0)
    return chapters


def apply_titles(chapters: list[Chapter], titles: dict[int, str]) -> list[Chapter]:
    """Replace chapter titles by 1-based chapter position (missing = keep)."""
    return [
        replace(c, title=titles[i]) if i in titles and titles[i] else c
        for i, c in enumerate(chapters, start=1)
    ]


def format_chapters(chapters: list[Chapter]) -> list[str]:
    """One ``M:SS Title`` line per chapter — the format YouTube parses."""
    return [f"{format_timestamp(c.start)} {c.title}" for c in chapters]


def chapter_issues(
    chapters: list[Chapter],
    total: float,
    *,
    min_chapter: float = MIN_CHAPTER,
) -> list[str]:
    """Why YouTube would not treat this list as chapters (empty = it will)."""
    issues: list[str] = []
    if len(chapters) < MIN_CHAPTER_COUNT:
        issues.append(
            f"only {len(chapters)} timestamp(s); YouTube needs at least {MIN_CHAPTER_COUNT}"
        )
    if chapters and chapters[0].start != 0.0:
        issues.append("first timestamp is not 0:00")
    for i, c in enumerate(chapters):
        if i and c.start <= chapters[i - 1].start:
            issues.append(f"timestamp {i + 1} is not after the previous one")
        end = chapters[i + 1].start if i + 1 < len(chapters) else total
        if end - c.start < min_chapter:
            issues.append(
                f"chapter {i + 1} ({format_timestamp(c.start)}) runs "
                f"{max(end - c.start, 0.0):.1f}s, under the {min_chapter:.0f}s minimum"
            )
    return issues
