"""Interval manipulation: merge, invert, apply margins, enforce constraints."""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple


def merge_intervals(
    intervals: Iterable[Tuple[float, float]], epsilon: float = 1e-6
) -> List[List[float]]:
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    if not sorted_intervals:
        return []

    merged: List[List[float]] = [[sorted_intervals[0][0], sorted_intervals[0][1]]]
    for start, end in sorted_intervals[1:]:
        last = merged[-1]
        if start <= last[1] + epsilon:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return merged


def invert_intervals(
    excludes: Sequence[Sequence[float]], duration_sec: float
) -> List[List[float]]:
    keeps: List[List[float]] = []
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
    intervals: List[dict],
    pre_margin: float,
    post_margin: float,
    duration_sec: float,
) -> List[dict]:
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
    keep_intervals: List[dict], captions: List[dict], duration_sec: float
) -> List[dict]:
    """Expand keep intervals so every caption has timeline overlap."""
    merged_input: List[Tuple[float, float]] = []

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
    keep_intervals: List[dict], min_keep: float, duration_sec: float
) -> List[dict]:
    """Ensure each keep interval is at least min_keep seconds long."""
    if min_keep <= 0.0:
        return keep_intervals

    expanded: List[Tuple[float, float]] = []
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
