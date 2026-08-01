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
from dataclasses import replace

from nagare_clip.director.director_llm import DirectorOp

logger = logging.getLogger(__name__)

SegTimes = list[tuple[float | None, float | None]]


def caption_duration(op: DirectorOp, seg_times: SegTimes) -> float | None:
    """On-screen seconds for *op*'s caption, or ``None`` when undeterminable.

    ``(end of the last line - start of the first line) / factor``.  Nothing
    inside the span is cut (the derived keep protects it), so this is the
    timelapse's real length on the edited timeline, not an estimate.  It is
    deliberately unclamped: every overlay shares one Blender channel, so a
    caption padded past its own span would collide with the next timelapse's.
    """
    a, b = op.lines
    factor = op.factor
    if not factor or factor <= 0:
        return None
    if a < 1 or b > len(seg_times):
        return None
    start = seg_times[a - 1][0]
    end = seg_times[b - 1][1]
    if start is None or end is None or end <= start:
        return None
    return round((end - start) / factor, 2)


def expand_timelapse_ops(ops: list[DirectorOp], seg_times: SegTimes) -> list[DirectorOp]:
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
        duration = caption_duration(op, seg_times)
        if op.text and duration is None:
            logger.warning(
                "guided_edit: timelapse [%d-%d] caption dropped: no usable segment times",
                a,
                b,
            )
        if op.text and duration is not None:
            out.append(replace(op, type="overlay", lines=(a, a), factor=None, duration=duration))
        out.append(replace(op, type="speed", lines=(a, b), text=None, duration=None))
        out.append(replace(op, type="keep", lines=(a, b), factor=None, text=None, duration=None))
    return out
