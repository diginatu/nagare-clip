"""Checks on the director's edit in display numbering, and the order.

Pure functions over a :class:`~nagare_clip.director.display.DisplayView`: the
playback order (``order`` ranges tiling the view, its breaks, its conversion
to and from source segments) and the per-op checks the agent skill's
``write`` tool reports — an op across a source join, a ``"n~"`` in display
numbering, a timelapse across a break of the order.

Pure: no I/O, no LLM.
"""

from __future__ import annotations

from typing import Any

from nagare_clip.director.display import DisplayView
from nagare_clip.director.ops import DirectorOp
from nagare_clip.order import Segment

#: A display range, 1-based inclusive.
Range = tuple[int, int]


def _joins(view: DisplayView, first: int, last: int) -> list[int]:
    """The display lines inside ``[first, last)`` a segment join follows."""
    return [n for n in range(first, last) if view.segment_join_after(n)]


def _pieces(view: DisplayView, first: int, last: int) -> list[tuple[int, int]]:
    """*[first, last]* cut into one range per segment it touches."""
    out: list[tuple[int, int]] = []
    start = first
    for join in _joins(view, first, last):
        out.append((start, join))
        start = join + 1
    out.append((start, last))
    return out


def _int(value: Any) -> int | None:
    return None if isinstance(value, bool) or not isinstance(value, int) else value


def _read_range(value: Any, total: int) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    first, last = _int(value[0]), _int(value[1])
    if first is None or last is None or not 1 <= first <= last <= total:
        return None
    return (first, last)


def read_order(value: Any, total: int) -> tuple[list[Range] | None, str]:
    """An ``order`` value as ranges tiling ``1..total``, or ``(None, why)``."""
    if not isinstance(value, list) or not value:
        return None, f'"order" must be a non-empty list of [first, last] ranges of 1-{total}.'
    ranges: list[Range] = []
    for item in value:
        span = _read_range(item, total)
        if span is None:
            return None, f'"order" entry {item!r} is not a [first, last] range of 1-{total}.'
        ranges.append(span)
    problems: list[str] = []
    cursor = 1
    for first, last in sorted(ranges):
        if first > cursor:
            problems.append(f"lines {cursor}-{first - 1} are in no range")
        elif first < cursor:
            problems.append(f"lines {first}-{min(last, cursor - 1)} are in more than one range")
        cursor = max(cursor, last + 1)
    if cursor <= total:
        problems.append(f"lines {cursor}-{total} are in no range")
    if problems:
        return None, (
            '"order" must cover every line exactly once — use cut to drop footage: '
            + "; ".join(problems)
            + "."
        )
    return ranges, ""


def order_breaks(order: list[Range], total: int) -> set[int]:
    """Display lines *y* after which the playback jumps elsewhere.

    A range ending at *y* is a break unless the range that plays next starts
    at ``y + 1`` — two ranges played back to back in view order are one run of
    footage.  The last line of the video is never a break.
    """
    breaks: set[int] = set()
    for i, (_first, last) in enumerate(order):
        if last >= total:
            continue
        following = order[i + 1][0] if i + 1 < len(order) else None
        if following != last + 1:
            breaks.add(last)
    return breaks


def order_segments(view: DisplayView, order: list[Range]) -> list[Segment] | None:
    """*order* in source coordinates, split at source boundaries.

    A range's first line is the speech line itself, or — for a silence line —
    the line after it (the silence before a line is its segment's by default).
    Its last line is a speech line, or a silence line, which becomes that line
    ``~`` (:attr:`~nagare_clip.order.Segment.gap_end`).  ``None`` when a piece
    holds no speech line at all.  Not normalised: the caller knows the counts.
    """
    out: list[Segment] = []
    for first, last in order:
        for a, b in _pieces(view, first, last):
            start, end = view.line(a), view.line(b)
            if start is None or end is None:  # pragma: no cover - ranges are validated
                return None
            begin = start.source_line + 1 if start.is_silence else start.source_line
            if begin > end.source_line:
                return None
            out.append(Segment(start.stem, (begin, end.source_line), end.is_silence))
    return out


def order_ranges(view: DisplayView, segments: list[Segment]) -> list[Range]:
    """Source-coordinate *segments* as display ranges, or ``[]`` if they don't map.

    The inverse of :func:`order_segments`, for mapping an ``order.json``
    back onto the view.  A segment starting at ``a`` takes the silence line
    before ``a`` unless another segment ends on it (``a-1~``).  Anything that
    does not tile the view comes back ``[]`` — shooting order.
    """
    claimed = {(s.stem, s.lines[1]) for s in segments if s.lines is not None and s.gap_end}
    by_stem: dict[str, list] = {}
    for seg in view.segments:
        by_stem.setdefault(seg.stem, []).append(seg)
    out: list[Range] = []
    for seg in segments:
        blocks = by_stem.get(seg.stem, [])
        if not blocks:
            return []
        if seg.lines is None:
            if len(blocks) != 1:
                return []
            out.append((blocks[0].first, blocks[0].last))
            continue
        a, b = seg.lines
        x = y = None
        for block in blocks:
            if a > 1 and (seg.stem, a - 1) not in claimed:
                x = x or view.from_source(block.index, a - 1, silence=True)
            x = x or view.from_source(block.index, a)
            if seg.gap_end:
                y = y or view.from_source(block.index, b, silence=True)
            y = y or view.from_source(block.index, b)
        if x is None or y is None or x > y:
            return []
        out.append((x, y))
    ranges, _why = read_order(out, len(view.lines))
    return ranges or []


def segment_joins(view: DisplayView, first: int, last: int) -> list[int]:
    """The display lines inside ``[first, last)`` a segment join follows."""
    return _joins(view, first, last)


def join_problem(
    view: DisplayView,
    op_type: str,
    first: int,
    last: int,
    splittable: frozenset[str] = frozenset(),
) -> str | None:
    """Why an op over ``[first, last]`` may not cross a segment join, or ``None``.

    Every crossing is refused (*splittable* names op types to let through;
    none by default), so the agent sees where the join is.
    """
    joins = _joins(view, first, last)
    if not joins or op_type in splittable:
        return None
    return (
        f"{op_type} [{first},{last}] crosses the segment join after line "
        f"{joins[0]}: the two sides are different footage. Send it as "
        "separate ops, one per side."
    )


def silence_ref_problem(op: DirectorOp) -> str | None:
    """Why *op* may not use the ``"n~"`` form in display numbering, or ``None``."""
    if not (op.gap_start or op.gap_end):
        return None
    return (
        f'{op.type} op lines {list(op.lines)} use the "n~" form; a silence '
        "is a numbered line of its own here — give its number."
    )


def break_problem(op_type: str, span: Range, breaks: set[int]) -> str | None:
    """Why a timelapse over display *span* may not stand under *breaks*, or ``None``."""
    if op_type != "timelapse":
        return None
    split = _split_by(span, breaks)
    if split is None:
        return None
    return (
        f"timelapse [{span[0]},{span[1]}] crosses the order's break after line "
        f"{split}: its two sides play apart. Move the break, or send one "
        "timelapse per side."
    )


def order_problems(
    view: DisplayView, value: Any, timelapses: list[Range]
) -> tuple[list[Range] | None, list[str]]:
    """An ``order`` value checked in full: ``(ranges, [])`` or ``(None, problems)``.

    It must tile the view (:func:`read_order`), no range may hold only a silence
    line, and no break may fall inside one of *timelapses* (display ranges).
    Every problem is listed, in that order.
    """
    total = len(view.lines)
    order, why = read_order(value, total)
    if order is None:
        return None, [why]
    if order_segments(view, order) is None:
        return None, [
            "a range holds only a silence line. A silence plays "
            "with the range that holds a line beside it."
        ]
    breaks = order_breaks(order, total)
    problems: list[str] = []
    for shown in timelapses:
        split = _split_by(shown, breaks)
        if split is not None:
            problems.append(
                f"its break after line {split} splits timelapse "
                f"[{shown[0]},{shown[1]}]. Move the break outside it, or rewrite the "
                "timelapse in the same reply."
            )
    return (None, problems) if problems else (order, [])


def _split_by(span: Range, breaks: set[int]) -> int | None:
    """The first break strictly inside *span*, if any."""
    return next((y for y in sorted(breaks) if span[0] <= y < span[1]), None)
