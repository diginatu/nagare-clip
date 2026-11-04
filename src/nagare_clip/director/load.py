"""Loading the director's view off disk: one segment's transcript and annotations.

:func:`load_segment_transcript` reads one segment's ``_edits.txt`` with its
timings, the silence the ``intervals`` stage drops from each line
(:func:`source_drops`), the silence lines and the described gaps — what
:func:`~nagare_clip.director.display.build_display_view` numbers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.config import DEFAULTS
from nagare_clip.director.context import project_context_block
from nagare_clip.director.display import DisplayView
from nagare_clip.director.silence_lines import (
    DEFAULT_SILENCE_LINE_MIN,
    SilenceLine,
    build_silence_lines,
)
from nagare_clip.director.transcript import speech_seconds
from nagare_clip.edit_lines import parse_edit_lines
from nagare_clip.edit_lines import silence_line_min as silence_line_min  # re-export
from nagare_clip.gap_context.context import anchor_gaps
from nagare_clip.gap_context.gaps import Gap, load_gaps
from nagare_clip.intervals.keep import dropped_ranges
from nagare_clip.order import Segment
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict
from nagare_clip.timing import segment_silences, segment_times


def _slice(values: list | None, first: int, last: int) -> list | None:
    return None if values is None else values[first - 1 : last]


@dataclass(frozen=True)
class SegmentInputs:
    """Where one segment's transcript and its timing annotations live."""

    segment: Segment
    edits: Path
    json_path: Path | None = None
    gaps: Path | None = None
    cuts_txt: Path | None = None
    #: ``director.silence_line_min``: the shortest wait between two lines the
    #: transcript shows as a silence line of its own.
    silence_line_min: float = DEFAULT_SILENCE_LINE_MIN
    #: The ``intervals:`` config section: a line's ``Ys silence`` is what that
    #: stage drops, so it is priced with that stage's settings.  ``None`` is
    #: the section's defaults.
    intervals_cfg: dict | None = None


@dataclass(frozen=True)
class SegmentTranscript:
    """One segment's lines and annotations, sliced to the lines it plays.

    ``seg_times``/``silences`` cover the segment only; ``gaps`` are anchored
    against the WHOLE source's times and then restricted to the segment, so one
    anchoring rule serves a whole source and a slice of one alike.

    ``silence_lines`` are the waits between this segment's lines long enough to
    be shown as lines of their own; the ``gaps`` left here are the descriptions
    none of them claimed (a silence INSIDE a line), still annotated above the
    line whose bracket reports it.
    """

    edit_lines: list[str]
    first_line: int
    seg_times: list | None
    silences: list | None
    gaps: list[tuple[int, Gap]]
    silence_lines: list[SilenceLine] = field(default_factory=list)

    def default_runtime(self) -> float | None:
        """Seconds the segment plays with no op at all; ``None`` if untimed."""
        if self.seg_times is None or len(self.seg_times) != len(self.edit_lines):
            return None
        speech = speech_seconds(self.seg_times, self.silences)
        if any(sec is None for sec in speech):
            return None
        return sum(speech)


_DROPS_CACHE: dict[str, list[tuple[float, float]]] = {}


def source_drops(
    data: dict,
    edit_lines: list[str],
    cuts_txt: Path | None,
    intervals_cfg: dict | None,
) -> list[tuple[float, float]]:
    """What ``intervals`` drops from this source with no director op.

    :func:`~nagare_clip.intervals.keep.dropped_ranges` over the same inputs the
    stage reads — the edit lines, the cut list, the ``intervals:`` settings —
    so a line's bracket prices exactly the footage the render drops.  Cached
    on those inputs: a source split into several segments is computed once.
    """
    cuts = read_cuts(Path(cuts_txt)) if cuts_txt and Path(cuts_txt).is_file() else []
    ivl = intervals_cfg if intervals_cfg is not None else DEFAULTS["intervals"]
    key = json.dumps([data, edit_lines, cuts, ivl], sort_keys=True, ensure_ascii=False)
    if key not in _DROPS_CACHE:
        try:
            drops = dropped_ranges(data, ivl, edit_lines=edit_lines, cut_ranges=cuts)
        except ValueError as exc:
            # intervals would refuse these edits outright; price the source
            # without them rather than not at all.
            logging.warning("director: edit lines do not sync (%s); pricing without them", exc)
            drops = dropped_ranges(data, ivl, cut_ranges=cuts)
        _DROPS_CACHE[key] = drops
    return _DROPS_CACHE[key]


def load_segment_transcript(inputs: SegmentInputs) -> SegmentTranscript:
    """Read one segment's transcript and annotations off disk.

    Every annotation is optional: a missing ``--json`` means no brackets, a
    missing cuts file no speech/silence split, a missing gaps file no gap lines.
    """
    segment = inputs.segment
    # Speech lines only: a silence line is not a transcript line, and counting
    # one would shift every line after it off its segment.
    all_lines = parse_edit_lines(
        inputs.edits.read_text(encoding="utf-8").splitlines()
    ).speech_lines()
    first, last = segment.lines or (1, len(all_lines))
    data: dict = {}
    seg_times = None
    if inputs.json_path and inputs.json_path.is_file():
        try:
            data = json.loads(inputs.json_path.read_text(encoding="utf-8"))
            seg_times = segment_times(data)
        except (ValueError, OSError):
            logging.warning("director: could not read --json %s", inputs.json_path)
    silences = None
    if seg_times:
        silences = segment_silences(
            seg_times, source_drops(data, all_lines, inputs.cuts_txt, inputs.intervals_cfg)
        )
    gap_list = (
        anchor_gaps(load_gaps(inputs.gaps), seg_times or [], segment.lines) if seg_times else []
    )
    silence_lines: list[SilenceLine] = []
    if seg_times:
        # The word times, not the segment bounds: this is the silence the
        # intervals stage drops and "n~" edits.
        silence_lines, gap_list = build_silence_lines(
            data,
            gap_list,
            min_seconds=inputs.silence_line_min,
            lines=(first, last),
        )
    return SegmentTranscript(
        edit_lines=all_lines[first - 1 : last],
        first_line=first,
        seg_times=_slice(seg_times, first, last),
        silences=_slice(silences, first, last),
        gaps=gap_list,
        silence_lines=silence_lines,
    )


#: Heads the whole-video transcript ``view`` prints.  Shooting order, whatever
#: order is in force: the order is the edit's, never the shape of this
#: transcript.
VIEW_HEADER = (
    "The whole video below, every source in shooting order under one "
    "numbering. [k] heads each source; your ops and your order address these numbers."
)


def project_context(summary: Path | None, view: DisplayView) -> str:
    """The summary stage's facts about the project, for the whole video.

    The ``plan`` stage's directions are deliberately NOT read: the director
    writes its own plan in its first turn.  A missing or empty ``summary.json``
    gives ``""``, which leaves the system message exactly as it was.
    """
    project_summary = ProjectSummary(summary="", parts=[])
    if summary and summary.is_file():
        project_summary = summary_from_dict(json.loads(summary.read_text(encoding="utf-8")))
    return project_context_block(project_summary, view)
