"""Desugar a director ``timelapse`` op into the markers that already exist.

A timelapse is a ``<keep><speed factor="F">…</speed></keep>`` span with one
caption over the whole of it.  Expressing that as three ops the director must
emit with matching ranges made the agreement unenforced — a keep that missed
the speed range by a line silently produced sped-up jump cuts.  The director
now emits one ``timelapse`` op and this module expands it, so the three markers
cannot disagree.

Pure: no I/O, no LLM call.  The caption's duration is derived rather than
stated — the derived ``<keep>`` preserves the whole span, so the edited-timeline
length of the timelapse is exactly ``span / factor``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace

from nagare_clip.director.director_llm import DirectorOp

logger = logging.getLogger(__name__)

SegTimes = list[tuple[float | None, float | None]]
#: ``{n: (start, end)}`` of the silence after line ``n`` (edit_lines.gap_spans).
Silences = Mapping[int, tuple[float, float]]


def caption_duration(
    op: DirectorOp, seg_times: SegTimes, silences: Silences | None = None
) -> float | None:
    """On-screen seconds for *op*'s caption, or ``None`` when undeterminable.

    ``(end of the last line - start of the first line) / factor``.  A ``"n~"``
    edge is the silence's own edge instead — where the marker on its silence
    line resolves to in the intervals stage — from *silences*.  Nothing
    inside the span is cut (the derived keep protects it), so this is the
    timelapse's real length on the edited timeline, not an estimate.  It is
    deliberately unclamped: every overlay shares one Blender channel, so a
    caption padded past its own span would collide with the next timelapse's.

    This assumes the *segment* times used here (``seg_times``) agree with the
    *word* times the derived ``<keep>``/``<speed>`` actually resolve to
    downstream (``intervals/sync_json.py::_resolve_keep_range`` uses
    ``first_word["start"]``/``last_word["end"]``).
    ``sentence_split.segment.segment_from_words`` sets segment bounds to the
    min/max over words that carry timings, so the two agree in the normal
    case — but can diverge when word times are non-monotonic or a boundary
    word is unaligned. If the segment end lands after the word end, the
    caption outlives its own sped span; that is the one condition the
    no-clamp decision above assumes cannot happen. The magnitude is bounded —
    at most ``(seg_end - word_end) / factor``, i.e. at most 0.25s per second
    of divergence at the ``TIMELAPSE_MIN_FACTOR`` floor — so this is a
    tolerated, rare edge case rather than one worth guarding against.
    """
    a, b = op.lines
    factor = op.factor
    if not factor or factor <= 0:
        return None
    if a < 1 or b > len(seg_times):
        return None
    silences = silences or {}
    if op.gap_start:
        start = silences[a][0] if a in silences else None
    else:
        start = seg_times[a - 1][0]
    if op.gap_end:
        end = silences[b][1] if b in silences else None
    else:
        end = seg_times[b - 1][1]
    if start is None or end is None or end <= start:
        return None
    return round((end - start) / factor, 2)


def expand_timelapse_ops(
    ops: list[DirectorOp], seg_times: SegTimes, silences: Silences | None = None
) -> list[DirectorOp]:
    """Replace every ``timelapse`` op with ``overlay`` + ``speed`` + ``keep``.

    Emission order matters: :func:`~nagare_clip.guided_edit.apply.apply_span_op`
    prepends each opening tag, so overlay-then-speed-then-keep yields
    ``<keep><speed factor="F"><overlay …/>`` on the first boundary line.

    Non-timelapse ops pass through untouched, in place.  When the caption's
    duration cannot be derived the span ops are still emitted: the continuity
    fix is the valuable half.
    """
    out: list[DirectorOp] = []
    for op in ops:
        if op.type != "timelapse":
            out.append(op)
            continue
        a, b = op.lines
        duration = caption_duration(op, seg_times, silences)
        if op.text and duration is None:
            logger.warning(
                "guided_edit: timelapse [%d-%d] caption dropped: no usable segment times",
                a,
                b,
            )
        if op.text and duration is not None:
            # A point at the span's opening: on the silence line when it opens
            # on one.
            out.append(
                replace(
                    op,
                    type="overlay",
                    lines=(a, a),
                    gap_end=op.gap_start,
                    factor=None,
                    duration=duration,
                )
            )
        out.append(replace(op, type="speed", lines=(a, b), text=None, duration=None))
        out.append(replace(op, type="keep", lines=(a, b), factor=None, text=None, duration=None))
    return out
