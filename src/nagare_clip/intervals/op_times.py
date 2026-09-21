"""Resolve director ops that address a silence into time ranges.

A ``keep``/``speed`` span is normally placed as a text marker in
``_edits.txt`` and resolved to "first word of the first line → last word of the
last line" (:mod:`nagare_clip.intervals.sync_json`).  That cannot address the
silence *between* two lines: there are no words there to wrap, and an opening
tag at a line's end falls forward while a closing tag at a line's start falls
back, so the range comes out inverted.

An op with a ``"n~"`` edge (:attr:`DirectorOp.gap_start` / ``gap_end``) is
therefore resolved here, to times, and handed to :func:`run_intervals`
alongside the marker-derived ranges.

The boundaries come from :func:`nagare_clip.intervals.speech.build_speech_spans`
— the very function whose gaps ``run_intervals`` drops — so a kept silence
starts and ends exactly where the dropping would have.  Reading a word's raw
``end`` instead would be wrong for the real project's 5 (of 49) gaps where a
stretched final word runs 6-26 s into the silence.

Pure: no I/O, no LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.intervals.speech import line_speech_spans

logger = logging.getLogger(__name__)

#: Ops resolved here.  ``cut`` stays on the marker path even with a silence
#: edge: keeps are grown back over caption words there (``intervals/run.py``),
#: and a time-only cut would lose that.  ``edit`` is placed by guided_edit's
#: LLM and has no span.
RESOLVED_TYPES = {"keep", "speed", "timelapse", "overlay"}


@dataclass(frozen=True)
class OpTimes:
    """Extra ranges for ``run_intervals``, in its own shapes.

    ``keeps`` are ``(start, end)``, ``speeds`` ``(start, end, factor)`` and
    ``overlays`` ``(start, duration, text)`` with *duration* in EDITED seconds
    — the same triples :func:`sync_json.extract_speed_ranges` and
    :func:`sync_json.extract_overlay_marks` produce, so the two sources merge
    without a conversion.
    """

    keeps: list[tuple[float, float]] = field(default_factory=list)
    speeds: list[tuple[float, float, float]] = field(default_factory=list)
    overlays: list[tuple[float, float, str]] = field(default_factory=list)


def _span_bounds(
    spans: list[list[tuple[float, float]]], op: DirectorOp
) -> tuple[float, float] | None:
    """The op's ``(start, end)`` in seconds, or ``None`` when unresolvable."""
    first, last = op.lines
    if not (1 <= first <= len(spans)) or not (1 <= last <= len(spans)):
        return None
    if not spans[first - 1] or not spans[last - 1]:
        return None

    if op.gap_start:
        # The silence AFTER `first` begins where its last word's span ends.
        start = spans[first - 1][-1][1]
    else:
        start = spans[first - 1][0][0]

    if op.gap_end:
        # ...and ends where the next line's first word begins.
        if last >= len(spans) or not spans[last]:
            return None
        end = spans[last][0][0]
    else:
        end = spans[last - 1][-1][1]

    if end <= start:
        return None
    return (start, end)


def resolve_op_times(ops: list[DirectorOp], whisperx_data: dict[str, Any]) -> OpTimes:
    """Time ranges for the ops that address a silence; the rest are left alone.

    An op that cannot be resolved (a line with no timed words, or a trailing
    silence on the source's last line, where there is no next line to end at)
    is skipped with a warning rather than guessed at.
    """
    out = OpTimes()
    spans = line_speech_spans(whisperx_data)
    for op in ops:
        if not (op.gap_start or op.gap_end) or op.type not in RESOLVED_TYPES:
            continue
        bounds = _span_bounds(spans, op)
        if bounds is None:
            logger.warning(
                "Director op not resolvable to times: %s %s (gap_start=%s, gap_end=%s)",
                op.type,
                list(op.lines),
                op.gap_start,
                op.gap_end,
            )
            continue
        start, end = bounds
        if op.type == "overlay":
            if op.text and op.duration and op.duration > 0:
                out.overlays.append((start, float(op.duration), op.text))
            continue
        if op.type == "keep":
            out.keeps.append((start, end))
            continue
        factor = op.factor
        if not factor or factor <= 0:
            logger.warning("Director %s op without a usable factor: %s", op.type, list(op.lines))
            continue
        out.speeds.append((start, end, float(factor)))
        if op.type == "timelapse":
            # A timelapse is keep + speed + one caption for the whole span; the
            # caption lasts the span's EDITED length, as guided_edit's
            # expansion computes it.
            out.keeps.append((start, end))
            if op.text:
                out.overlays.append((start, (end - start) / factor, op.text))
    return out
