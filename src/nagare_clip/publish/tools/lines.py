"""Display line -> source span -> finished-timeline time (pure, no LLM)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from nagare_clip.director.tools.project import Project
from nagare_clip.pipeline.stages import ordered_sources_for
from nagare_clip.publish.timeline import Placement, build_placements, first_surviving_time


@dataclass(frozen=True)
class LineSpan:
    """A display line's footage: its source and source seconds."""

    stem: str
    start: float
    end: float


def line_span(project: Project, number: int) -> LineSpan | None:
    """Source seconds of display line *number*; ``None`` when out of range or untimed."""
    if not 1 <= number <= len(project.view.lines):
        return None
    line = project.view.lines[number - 1]
    transcript = project.transcripts[line.segment - 1]
    times = transcript.seg_times
    if times is None:
        return None
    if not line.is_silence:
        offset = line.source_line - transcript.first_line
        if not 0 <= offset < len(times):
            return None
        start, end = times[offset]
        if start is None or end is None:
            return None
        return LineSpan(line.stem, float(start), float(end))
    for silence in transcript.silence_lines:
        if silence.after_line == line.source_line:
            return LineSpan(line.stem, float(silence.start), float(silence.end))
    return None


def line_time(placements: Sequence[Placement], project: Project, number: int) -> float | None:
    """Finished-timeline seconds of the first surviving moment of line *number*."""
    span = line_span(project, number)
    if span is None:
        return None
    return first_surviving_time(placements, span.stem, span.start, span.end)


def finished_placements(project: Project) -> list[Placement] | None:
    """The finished timeline's placements; ``None`` when no source has intervals."""
    sources = ordered_sources_for(project.ctx)
    if not sources:
        return None
    return build_placements(sources)
