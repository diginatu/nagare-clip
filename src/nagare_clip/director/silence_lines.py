"""The waits between transcript lines, as lines of the transcript themselves.

A no-speech stretch between two lines used to reach the director only as a
number inside the preceding line's bracket (``gap 29.9s``) with the vision
description printed above the line as ``[silent gap: …]``.  Nothing in that
view said the wait was addressable, and nothing could be said about it: an op
addresses lines, and the silence was not one.

Here it becomes its own — deliberately UN-numbered — line naming its anchor::

    53: その状態で  [0.9s]
        [silent 29.9s after line 53: a hand enters from the right …]
    54: この状態で今予備水持ってきたんで…  [16.4s speech, 3.1s silence]

so the director can write ``"53~"`` (:attr:`DirectorOp.gap_start` /
``gap_end``) for exactly that stretch.  It stays un-numbered because every
existing line number — the plan's directions, ``_director.json``,
``guided_edit``, ``intervals`` — is a SOURCE line number, and numbering the
silences would shift all of them; increment 3 renumbers globally instead.

The interval is :func:`nagare_clip.intervals.speech.line_speech_spans`'s, i.e.
the very silence ``run_intervals`` drops and a marker on the silence line
after ``n`` in ``_edits.txt`` resolves to (guided_edit writes ``"n~"`` there,
through :meth:`SilenceLine.body`) — so what the director reads and what it
edits cannot be two different stretches of footage.

Pure: no I/O, no LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from nagare_clip.edit_lines import (
    DEFAULT_SILENCE_LINE_MIN,
    JOIN,
    expected_silences,
    gap_spans,
    silence_body,
)
from nagare_clip.gap_context.gaps import Gap

__all__ = [
    "DEFAULT_SILENCE_LINE_MIN",
    "INDENT",
    "JOIN",
    "SilenceLine",
    "build_silence_lines",
    "gap_spans",
    "silence_body",
]

#: Silence lines and gap annotations are indented by this much, the same as
#: :mod:`nagare_clip.gap_context.context`'s annotations, so neither can be
#: mistaken for a numbered line.
INDENT = "    "


@dataclass(frozen=True)
class SilenceLine:
    """One wait between two lines: where it is, how long, what is visible.

    *after_line* is the 1-based SOURCE line the silence follows — the ``n`` of
    the ``"n~"`` that addresses it.
    """

    after_line: int
    start: float
    end: float
    descriptions: tuple[str, ...] = field(default=())

    @property
    def duration(self) -> float:
        return self.end - self.start

    def body(self) -> str:
        """``[silent 29.9s: …]`` — this silence as a line with a place of its own.

        The one text for it wherever it has one: the director's whole-video view
        numbers it (:func:`~nagare_clip.director.display.build_display_view`)
        and guided_edit writes it into ``_edits.txt`` verbatim, so an op placed
        on the line the director read lands on a line that reads the same.
        """
        return silence_body(self.duration, self.descriptions)

    def render(self, after_line: int | None = None) -> str:
        """The transcript line, indented and un-numbered.

        The duration is stated HERE and nowhere else: the preceding line's
        bracket drops its ``gap`` part when a silence line follows it
        (:func:`~nagare_clip.director.director_llm.format_numbered_transcript_timed`),
        so one silence never shows up as two numbers.

        *after_line* overrides the line number named, for a view that numbers
        its lines differently (increment 3's whole-video display numbering);
        the default names this silence's own source line.
        """
        anchor = self.after_line if after_line is None else after_line
        return f"{INDENT}{silence_body(self.duration, self.descriptions, after_line=anchor)}"


def build_silence_lines(
    whisperx_data: dict[str, Any],
    anchored_gaps: Sequence[tuple[int, Gap]] = (),
    *,
    min_seconds: float = DEFAULT_SILENCE_LINE_MIN,
    lines: tuple[int, int] | None = None,
) -> tuple[list[SilenceLine], list[tuple[int, Gap]]]:
    """This source's silence lines, and the gap annotations none of them claimed.

    *anchored_gaps* are :func:`nagare_clip.gap_context.context.anchor_gaps`'s
    pairs; only each gap's TIMES are read here, so the anchors may be
    segment-relative or absolute alike.  A gap is claimed by the silence line
    whose interval contains its midpoint — the same midpoint rule
    ``anchor_gaps`` uses, and silence lines do not overlap, so at most one
    claims it.  Everything else is returned unchanged for the caller to render
    the way it always did: on the real project 12 of 59 descriptions describe a
    silence INSIDE a line, which the line's own ``Ys silence`` reports and no
    ``"n~"`` can address.

    ``lines=(a, b)`` restricts the result to one segment: the silence after its
    last line leads into the segment that plays NEXT (``anchor_gaps``' rule),
    and one before its first line is unaddressable from inside it, so both are
    left out.
    """
    spans = gap_spans(whisperx_data)
    shown = expected_silences(whisperx_data, min_seconds)
    first, last = lines if lines is not None else (1, max(spans, default=0) + 1)
    claimed: set[int] = set()
    out: list[SilenceLine] = []
    for after_line in sorted(spans):
        if not (first <= after_line < last):
            continue
        if after_line not in shown:
            continue
        start, end = spans[after_line]
        held = [
            (i, gap)
            for i, (_anchor, gap) in enumerate(anchored_gaps)
            if start <= (gap.start + gap.end) / 2 <= end
        ]
        claimed.update(i for i, _ in held)
        out.append(
            SilenceLine(
                after_line=after_line,
                start=start,
                end=end,
                descriptions=tuple(
                    gap.description for _, gap in sorted(held, key=lambda h: h[1].start)
                ),
            )
        )
    leftover = [pair for i, pair in enumerate(anchored_gaps) if i not in claimed]
    return out, leftover
