"""The project's brief for the whole finished video, for the director's prefix.

Lives in the ``director`` package (not ``director_llm``) because it depends on
the ``summary`` and ``plan`` stages; ``director_llm`` stays free of those imports
so ``summary`` can keep importing it without a cycle.

One conversation reads the whole video under one numbering
(:mod:`nagare_clip.director.display`), so this block is rendered once, for
every segment at once, and every line range in it is a DISPLAY number.  What
the per-segment version also carried is gone with that path: the seam lines at
each join (the view now contains both sides of every join), the ops already
made to earlier segments (one conversation remembers its own), the captions
already shown, and the "Earlier/Later in the finished video" sibling summaries
(the view carries those segments' transcripts in full).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from nagare_clip.director.display import DisplayView
from nagare_clip.order import Segment
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.summary.summarize import ProjectSummary

logger = logging.getLogger(__name__)

#: Travels WITH the plan's line ranges, immediately above them, wherever they
#: are rendered.  A measured 9-segment run copied plan part boundaries into 19
#: of 56 op starts and into all three timelapses -- two of which therefore
#: opened on the line where the speaker announces the work and played that
#: announcement at 8-20x, unintelligible.  The rule that should have prevented
#: it sits in ``DIRECTOR_PROMPT``'s Rules block at ~55% of the assembled
#: prompt, and the more concrete, later ranges won; so the correction is
#: emitted here and nowhere else.  It also names the literal word "keep",
#: which ``PLAN_PROMPT`` forbids in a direction and 7 of 9 real directions used
#: anyway, and which the director's own guardrail (featured/retained/
#: emphasised) does not cover.
SECTION_BOUNDARY_NOTE = (
    "These are section boundaries, not op boundaries: a direction "
    "says what a stretch is FOR, and you choose where each op "
    'actually starts and ends. A direction saying "keep" means '
    "emphasis, not a keep op."
)


def _overlaps(part, segment: Segment) -> bool:
    """Does *part* cover any line this segment plays?"""
    if part.stem != segment.stem:
        return False
    if segment.lines is None:
        return True
    return part.lines[0] <= segment.lines[1] and part.lines[1] >= segment.lines[0]


def _clipped(part_lines: tuple[int, int], segment: Segment) -> tuple[int, int]:
    """A part's range as this segment sees it — it plays no more than its own."""
    if segment.lines is None:
        return part_lines
    return (max(part_lines[0], segment.lines[0]), min(part_lines[1], segment.lines[1]))


def _direction_rows(
    directions: list[PartDirection],
    segment: Segment,
    index: int,
    view: DisplayView,
) -> list[str]:
    """One segment's directions, ranged in DISPLAY numbers.

    Each direction is clipped to the lines this segment actually plays and then
    mapped through :meth:`DisplayView.from_source` -- the model reads one global
    numbering, and a source number rendered into it would name unrelated
    footage.  A range the view cannot map (a plan that claims more lines than
    the transcript has) is skipped with a warning rather than rendered raw.
    """
    rows: list[tuple[int, str]] = []
    for direction in sorted(directions, key=lambda d: d.lines):
        if not _overlaps(direction, segment):
            continue
        first, last = _clipped(direction.lines, segment)
        if first > last:
            continue
        start = view.from_source(index, first)
        end = view.from_source(index, last)
        if start is None or end is None:
            logger.warning(
                "director: no display line for %s lines %d-%d; direction skipped (%s)",
                direction.stem,
                direction.lines[0],
                direction.lines[1],
                direction.direction,
            )
            continue
        rows.append((start, f"- lines {start}-{end}: {direction.direction}"))
    return [row for _, row in sorted(rows, key=lambda item: item[0])]


def project_context_block(
    project_summary: ProjectSummary,
    directions: list[PartDirection],
    segments: Sequence[Segment],
    view: DisplayView,
) -> str:
    """The project's context for the WHOLE video, for the cacheable prefix.

    The overall summary once, then every segment in playback order with the
    plan's directions covering it, every range in the display numbering the
    conversation reads -- and :data:`SECTION_BOUNDARY_NOTE` immediately above
    that list, which is the only place it is emitted.

    *segments* is the playback order, positionally the same as ``view.segments``
    (``[k]`` is a position in both).

    ``""`` when there is nothing to say, so an empty overview leaves the system
    message exactly as it was.
    """
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        label = view.segments[index - 1].label if index <= len(view.segments) else "?"
        rows = _direction_rows(directions, segment, index, view)
        blocks.append("\n".join([f"[{index}] {label}:", *(rows or ["- (no directions)"])]))
    listed = any("- lines " in block for block in blocks)

    out: list[str] = []
    if project_summary.summary:
        out.append(f"Project context (all videos):\nOverall: {project_summary.summary}")
    if listed:
        out.append("\n".join([SECTION_BOUNDARY_NOTE, *blocks]))
    return "\n\n".join(out)
