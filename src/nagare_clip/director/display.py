"""The whole finished video as ONE numbered transcript, and back again.

Until now the director read one segment per call, each numbered in its own
SOURCE coordinates — line 31 of a segment that starts at 31.  Nine calls, nine
numberings, and a reference block that had to qualify every number with its
segment index (``[2]47``) so a copied number could not silently edit unrelated
footage.

Here the finished video is numbered ONCE, 1..N in playback order, speech lines
and silence lines together::

    [1] PXL_20260502_085157585 [1-30]
    1: 今日はポンプを直します  [3.2s]
    2: まずタンクを外して  [2.1s]
    3: [silent 29.9s: a hand enters from the right holding a clear tube]
    4: この状態で  [0.9s]

    [2] PXL_20260426_090431216
    5: ...

so the model names one range per turn and the code converts it back to the
per-source coordinates ``_director.json``, ``guided_edit`` and ``intervals``
have always spoken (:meth:`DisplayView.to_source`).  A silence line at an edge
of a range is exactly increment 1's ``"n~"``: the silence AFTER the source line
it follows, never the line after it.

A range that crosses a segment join is refused rather than clipped: the two
sides are different footage, so an op over both means nothing — the caller
(:mod:`nagare_clip.director.loop`) splits a ``cut``/``keep`` at the join and
refuses the rest.  One source may play as several segments, and not in source
order (``PXL_20260502_085157585`` plays 1-30, then 84-97, then 31-83), so
"same stem" never means "same segment" here.

The silence lines are increment 2's — :func:`~.silence_lines.build_silence_lines`
decides which waits qualify and which gap descriptions they carry, once, in
:func:`~.run.load_segment_transcript`; this module only numbers what it built.

Pure: no I/O, no LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nagare_clip.director.director_llm import (
    clean_for_display,
    format_numbered_transcript,
    format_numbered_transcript_timed,
)
from nagare_clip.order import Segment, segment_label

if TYPE_CHECKING:  # pragma: no cover - typing only, and run.py may import us
    from nagare_clip.director.run import SegmentTranscript

#: Annotation lines are indented and un-numbered, as they are everywhere else
#: the director reads them, so neither can be mistaken for a display line.
INDENT = "    "


@dataclass(frozen=True)
class DisplayLine:
    """One line of the finished video's single numbering.

    *source_line* is the speech line's own number in its source — or, for a
    silence line, the line the silence FOLLOWS (the ``n`` of ``"n~"``).
    *annotations* are gap descriptions no silence line claimed; they are shown
    under this line, un-numbered, exactly as they were before.
    """

    number: int
    segment: int
    stem: str
    source_line: int
    is_silence: bool
    text: str
    annotations: tuple[str, ...] = ()


@dataclass(frozen=True)
class DisplaySegment:
    """One stretch of footage in the playback order, and where it sits."""

    index: int
    stem: str
    label: str
    first: int
    last: int
    #: Descriptions anchored BEFORE the segment's first line (anchor ``0``).
    leading: tuple[str, ...] = ()


@dataclass(frozen=True)
class DisplayView:
    """The numbered whole video, plus the map back to per-source coordinates."""

    lines: list[DisplayLine] = field(default_factory=list)
    segments: list[DisplaySegment] = field(default_factory=list)

    def render(self) -> str:
        """The transcript the director reads: every segment, every line, once."""
        blocks: list[str] = []
        for seg in self.segments:
            out = [f"[{seg.index}] {seg.label}"]
            out.extend(f"{INDENT}[silent gap: {text}]" for text in seg.leading)
            for line in self.lines[seg.first - 1 : seg.last]:
                out.append(f"{line.number}: {line.text}".rstrip())
                out.extend(f"{INDENT}[silent gap: {text}]" for text in line.annotations)
            blocks.append("\n".join(out))
        return "\n\n".join(blocks)

    def line(self, number: int) -> DisplayLine | None:
        if not 1 <= number <= len(self.lines):
            return None
        return self.lines[number - 1]

    def to_source(self, first: int, last: int) -> tuple[str, tuple[int, int], bool, bool] | None:
        """``(stem, (source first, source last), gap_start, gap_end)``.

        ``None`` when the range is not a range of this video, or when it
        crosses a segment join — the two sides are different footage.
        """
        a, b = self.line(first), self.line(last)
        if a is None or b is None or first > last or a.segment != b.segment:
            return None
        return (a.stem, (a.source_line, b.source_line), a.is_silence, b.is_silence)

    def segment_join_after(self, number: int) -> bool:
        """Is there a cut to other footage between *number* and the next line?

        False at the very last line: the video ends there, and an end is not a
        join — nothing can span it.
        """
        here, nxt = self.line(number), self.line(number + 1)
        return here is not None and nxt is not None and here.segment != nxt.segment

    def from_source(self, segment: int, source_line: int, *, silence: bool = False) -> int | None:
        """The display number of one segment's source line (or its silence)."""
        for line in self.lines:
            if (
                line.segment == segment
                and line.source_line == source_line
                and line.is_silence == silence
            ):
                return line.number
        return None


def _bodies(transcript: SegmentTranscript) -> list[str]:
    """Each source line rendered as the director sees it, without its number.

    The renderer is the segment's own
    (:func:`~.director_llm.format_numbered_transcript_timed`), so a line's
    bracket here and in a per-segment view cannot drift; only the number in
    front of it changes.  A line's number never contains ``": "``, so the split
    is exact.
    """
    clean = clean_for_display(transcript.edit_lines)
    seg_times = transcript.seg_times
    if seg_times is not None and len(seg_times) == len(clean):
        block = format_numbered_transcript_timed(
            clean,
            seg_times,
            silences=transcript.silences,
            first_line=transcript.first_line,
            silence_after={line.after_line for line in transcript.silence_lines},
        )
    else:
        block = format_numbered_transcript(clean, first_line=transcript.first_line)
    return [text.split(": ", 1)[1] if ": " in text else "" for text in block.split("\n")]


def _silence_text(descriptions: Sequence[str], seconds: float) -> str:
    """A silence line's body.  It has a number of its own now, so it no longer
    says which line it follows — that is the line above it."""
    body = f"silent {seconds:.1f}s"
    if descriptions:
        body += ": " + " / ".join(descriptions)
    return f"[{body}]"


def build_display_view(
    segments: Sequence[tuple[Segment, SegmentTranscript]],
) -> DisplayView:
    """Number every segment's lines, in playback order, from 1.

    *segments* are the finished video's segments in playback order, each with
    the transcript :func:`~.run.load_segment_transcript` loaded for it — so the
    silence lines, the brackets and the gap descriptions are the ones increment
    2 already decided on.
    """
    lines: list[DisplayLine] = []
    blocks: list[DisplaySegment] = []
    for index, (segment, transcript) in enumerate(segments, start=1):
        bodies = _bodies(transcript)
        silences = {line.after_line: line for line in transcript.silence_lines}
        annotations: dict[int, list[str]] = {}
        for anchor, gap in transcript.gaps:
            annotations.setdefault(anchor, []).append(gap.description)
        first = len(lines) + 1
        for offset, body in enumerate(bodies):
            source_line = transcript.first_line + offset
            lines.append(
                DisplayLine(
                    number=len(lines) + 1,
                    segment=index,
                    stem=segment.stem,
                    source_line=source_line,
                    is_silence=False,
                    text=body,
                    annotations=tuple(annotations.get(offset + 1, ())),
                )
            )
            silence = silences.get(source_line)
            if silence is not None:
                lines.append(
                    DisplayLine(
                        number=len(lines) + 1,
                        segment=index,
                        stem=segment.stem,
                        source_line=source_line,
                        is_silence=True,
                        text=_silence_text(silence.descriptions, silence.duration),
                    )
                )
        blocks.append(
            DisplaySegment(
                index=index,
                stem=segment.stem,
                label=segment_label(segment),
                first=first,
                last=len(lines),
                leading=tuple(annotations.get(0, ())),
            )
        )
    return DisplayView(lines=lines, segments=blocks)
