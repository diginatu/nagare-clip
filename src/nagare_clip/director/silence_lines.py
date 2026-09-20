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
the very silence ``run_intervals`` drops and :mod:`nagare_clip.intervals.
op_times` resolves ``"n~"`` to — so what the director reads and what it edits
cannot be two different stretches of footage.

Pure: no I/O, no LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from nagare_clip.gap_context.gaps import Gap
from nagare_clip.intervals.speech import line_speech_spans

#: Default for ``director.silence_line_min``: the shortest wait shown as its
#: own line.  Matches ``gap_context.min_gap``, so every described gap has a
#: silence line to land in.
DEFAULT_SILENCE_LINE_MIN = 5.0

#: Silence lines and gap annotations are indented by this much, the same as
#: :mod:`nagare_clip.gap_context.context`'s annotations, so neither can be
#: mistaken for a numbered line.
INDENT = "    "

#: Joins several descriptions anchored in one silence (11 of the real
#: project's 30 described silences carry more than one).
JOIN = " / "


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


def silence_body(
    seconds: float, descriptions: Sequence[str] = (), after_line: int | None = None
) -> str:
    """``[silent 29.9s: …]`` — the bracket every view of a silence prints.

    The one formatter: the un-numbered transcript line (:meth:`SilenceLine.
    render`), the numbered display line
    (:func:`~nagare_clip.director.display.build_display_view`) and the playback
    preview all render a silence through it, so its seconds and its
    descriptions cannot drift between the three.  *after_line* is included only
    where the silence has no number of its own to be addressed by.
    """
    body = f"silent {seconds:.1f}s"
    if after_line is not None:
        body += f" after line {after_line}"
    if descriptions:
        body += ": " + JOIN.join(descriptions)
    return f"[{body}]"


def gap_spans(whisperx_data: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """``{line: (start, end)}`` for the silence after every line that has one.

    Every between-line gap, at any length — the threshold is a display
    decision, made in :func:`build_silence_lines`, while a hand-written op may
    address a shorter one and the playback preview still has to price it.
    """
    spans = line_speech_spans(whisperx_data)
    out: dict[int, tuple[float, float]] = {}
    for i in range(len(spans) - 1):
        if not spans[i] or not spans[i + 1]:
            continue
        start, end = spans[i][-1][1], spans[i + 1][0][0]
        if end > start:
            out[i + 1] = (start, end)
    return out


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
    first, last = lines if lines is not None else (1, max(spans, default=0) + 1)
    claimed: set[int] = set()
    out: list[SilenceLine] = []
    for after_line in sorted(spans):
        if not (first <= after_line < last):
            continue
        start, end = spans[after_line]
        if end - start < min_seconds:
            continue
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


def insert_silence_lines(transcript: str, silence_lines: Sequence[SilenceLine]) -> str:
    """Put each silence line after the numbered line it follows.

    Works on the RENDERED transcript (the same positional trick
    :func:`~nagare_clip.gap_context.context.annotate_numbered_transcript` uses)
    and after it, so a line's own annotations stay attached to it and the
    silence that follows the line comes last.  A silence line whose anchor is
    not in the transcript is dropped rather than moved.
    """
    if not silence_lines:
        return transcript
    by_anchor = {line.after_line: line for line in silence_lines}
    out: list[str] = []
    current: int | None = None

    def flush() -> None:
        if current is not None and current in by_anchor:
            out.append(by_anchor[current].render())

    for text in transcript.split("\n"):
        number = _line_number(text)
        if number is not None:
            flush()
            current = number
        out.append(text)
    flush()
    return "\n".join(out)


def _line_number(text: str) -> int | None:
    head = text.split(":", 1)[0]
    return int(head) if head.isdigit() else None
