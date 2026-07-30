"""Interval manipulation: merge, invert, apply margins, enforce constraints."""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def merge_intervals(
    intervals: Iterable[tuple[float, float]], epsilon: float = 1e-6
) -> list[list[float]]:
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    if not sorted_intervals:
        return []

    merged: list[list[float]] = [[sorted_intervals[0][0], sorted_intervals[0][1]]]
    for start, end in sorted_intervals[1:]:
        last = merged[-1]
        if start <= last[1] + epsilon:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return merged


def subtract_intervals(
    base: Iterable[tuple[float, float]],
    cuts: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Carve *cuts* out of each interval in *base*.

    Each base interval is shortened or split where a cut overlaps it.  Cuts
    that merely touch a boundary (`cut.end == base.start` or
    `cut.start == base.end`) are no-ops.  Zero-length residues are dropped.
    Output is sorted by start.
    """
    merged_cuts = merge_intervals(cuts)
    result: list[tuple[float, float]] = []

    for b_start, b_end in sorted(base, key=lambda x: x[0]):
        cursor = b_start
        for c_start, c_end in merged_cuts:
            if c_end <= cursor:
                continue
            if c_start >= b_end:
                break
            if c_start > cursor:
                result.append((cursor, min(c_start, b_end)))
            cursor = max(cursor, c_end)
            if cursor >= b_end:
                break
        if cursor < b_end:
            result.append((cursor, b_end))

    return result


def invert_intervals(excludes: Sequence[Sequence[float]], duration_sec: float) -> list[list[float]]:
    keeps: list[list[float]] = []
    cursor = 0.0
    for start, end in excludes:
        start_f = max(0.0, min(float(start), duration_sec))
        end_f = max(0.0, min(float(end), duration_sec))
        if start_f > cursor:
            keeps.append([cursor, start_f])
        cursor = max(cursor, end_f)
    if cursor < duration_sec:
        keeps.append([cursor, duration_sec])
    return keeps


def apply_margins(
    intervals: list[dict],
    pre_margin: float,
    post_margin: float,
    duration_sec: float,
) -> list[dict]:
    """
    Expand each interval by pre_margin before start and post_margin
    after end, clamp to [0, duration_sec], then merge overlaps.
    """
    if not intervals:
        return intervals

    expanded = []
    for iv in intervals:
        start = max(0.0, iv["start"] - pre_margin)
        end = min(duration_sec, iv["end"] + post_margin)
        expanded.append({"start": start, "end": end})

    expanded.sort(key=lambda x: x["start"])

    merged = [expanded[0]]
    for iv in expanded[1:]:
        if iv["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], iv["end"])
        else:
            merged.append(iv)

    return merged


def ensure_keep_covers_captions(
    keep_intervals: list[dict], captions: list[dict], duration_sec: float
) -> list[dict]:
    """Expand keep intervals so every caption has timeline overlap."""
    merged_input: list[tuple[float, float]] = []

    for iv in keep_intervals:
        start = max(0.0, min(float(iv["start"]), duration_sec))
        end = max(0.0, min(float(iv["end"]), duration_sec))
        if end > start:
            merged_input.append((start, end))

    for cap in captions:
        start = max(0.0, min(float(cap["start"]), duration_sec))
        end = max(0.0, min(float(cap["end"]), duration_sec))
        if end > start:
            merged_input.append((start, end))

    merged = merge_intervals(merged_input)
    return [{"start": round(start, 3), "end": round(end, 3)} for start, end in merged]


def enforce_min_keep_duration(
    keep_intervals: list[dict], min_keep: float, duration_sec: float
) -> list[dict]:
    """Ensure each keep interval is at least min_keep seconds long."""
    if min_keep <= 0.0:
        return keep_intervals

    expanded: list[tuple[float, float]] = []
    for iv in keep_intervals:
        start = max(0.0, min(float(iv["start"]), duration_sec))
        end = max(0.0, min(float(iv["end"]), duration_sec))
        if end <= start:
            continue

        length = end - start
        if length < min_keep:
            missing = min_keep - length
            grow_before = missing / 2.0
            grow_after = missing - grow_before
            start = max(0.0, start - grow_before)
            end = min(duration_sec, end + grow_after)

            length = end - start
            if length < min_keep:
                if start <= 0.0:
                    end = min(duration_sec, start + min_keep)
                elif end >= duration_sec:
                    start = max(0.0, end - min_keep)

        expanded.append((start, end))

    merged = merge_intervals(expanded)
    return [{"start": round(start, 3), "end": round(end, 3)} for start, end in merged]


def merge_close_intervals(keep_intervals: list[dict], min_cut: float) -> list[dict]:
    """Absorb gaps between adjacent keep intervals shorter than *min_cut*.

    Every other post-processor here constrains the intervals; this one
    constrains the gaps between them.  Keep/caption margins eat into an
    exclude gap from both sides, so a cut can survive as a millisecond
    sliver — a visible jump cut that saves no runtime.  A gap exactly at
    the threshold survives; absorption is transitive, so a chain of
    slivers collapses into one interval.

    Input must be sorted and disjoint (the earlier passes guarantee it).
    ``min_cut <= 0`` disables the pass, returning the input unchanged.
    """
    if min_cut <= 0.0 or not keep_intervals:
        return keep_intervals

    merged = [dict(keep_intervals[0])]
    for iv in keep_intervals[1:]:
        if float(iv["start"]) - float(merged[-1]["end"]) < min_cut:
            merged[-1]["end"] = max(float(merged[-1]["end"]), float(iv["end"]))
        else:
            merged.append(dict(iv))
    return merged


def snap_overlay_starts(
    overlays: Sequence[tuple[float, float, str]],
    line_spans: Sequence[tuple[float | None, float | None]],
    keep_intervals: Sequence[dict],
) -> list[tuple[float, float, str]]:
    """Move an overlay anchor that landed on cut footage onto surviving footage.

    An ``<overlay/>`` is a point marker at the start of a transcript line, and
    a line's opening seconds are routinely cut (silence detection reaches them,
    keep margins do not reach back far enough).  The blender stage drops an
    overlay whose anchor is on no keep interval, so a 2-second miss used to
    cost the whole caption.  Here the anchor is snapped to the first surviving
    moment **of its own line** instead: the earliest keep interval that
    overlaps the line, clamped to the line's own start so a keep interval
    reaching in from the previous line can't pull the caption backwards.  A
    later surviving chunk wins over an earlier one when the anchor sits between
    them — the caption moves forward with the words it belongs to.

    ``duration`` is never touched: it is stated reading time on the edited
    timeline, so clipping it to the line's surviving footage would re-introduce
    exactly the derived-length problem the point marker replaced.  A caption is
    allowed to run on over whatever follows (the blender stage still clamps it
    to the end of the source's timeline).

    An overlay is returned unchanged when its line has no surviving footage at
    all, when no line contains the anchor, or when the line has no timing —
    there is genuinely nowhere to put it, and the blender stage's skip warning
    is left to fire.

    Containment is half-open (``start <= t < end``) to match the blender
    stage's ``tl_map`` lookup, so an anchor exactly on a keep interval's end is
    snapped rather than left to be dropped.
    """
    if not overlays or not keep_intervals:
        return list(overlays)

    ivs = sorted(
        (float(iv["start"]), float(iv["end"]))
        for iv in keep_intervals
        if float(iv["end"]) > float(iv["start"])
    )

    snapped: list[tuple[float, float, str]] = []
    for start, duration, text in overlays:
        if any(iv_start <= start < iv_end for iv_start, iv_end in ivs):
            snapped.append((start, duration, text))
            continue

        line = _line_containing(line_spans, start)
        if line is None:
            snapped.append((start, duration, text))
            continue
        line_start, line_end = line

        covering = [
            (iv_start, iv_end)
            for iv_start, iv_end in ivs
            if iv_start < line_end and iv_end > line_start
        ]
        if not covering:
            snapped.append((start, duration, text))
            continue

        forward = [iv for iv in covering if iv[0] > start]
        chosen = forward[0] if forward else covering[0]
        snapped.append((max(chosen[0], line_start), duration, text))

    return snapped


def _line_containing(
    line_spans: Sequence[tuple[float | None, float | None]], t: float
) -> tuple[float, float] | None:
    """First ``(start, end)`` line span containing *t* (inclusive), if any."""
    for start, end in line_spans:
        if start is None or end is None:
            continue
        if start <= t <= end:
            return (float(start), float(end))
    return None
