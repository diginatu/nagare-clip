"""Caption chunking from bunsetsu-level timing data."""

from __future__ import annotations

import logging
import math
from typing import List, Tuple


def expand_short_captions(
    captions: List[dict],
    min_duration: float,
    duration_sec: float,
) -> List[dict]:
    """Expand captions shorter than min_duration symmetrically.

    Expansion is clamped to:
    - the previous caption's end (or 0.0 for the first caption)
    - the next caption's start (or duration_sec for the last caption)

    Unused margin on one side is redistributed to the other side.
    """
    if min_duration <= 0.0 or not captions:
        return captions

    result = []
    n = len(captions)
    for i, cap in enumerate(captions):
        start = float(cap["start"])
        end = float(cap["end"])
        speech_dur = end - start

        if speech_dur >= min_duration:
            result.append(cap)
            continue

        deficit = min_duration - speech_dur
        half = deficit / 2.0

        lo = float(captions[i - 1]["end"]) if i > 0 else 0.0
        hi = float(captions[i + 1]["start"]) if i < n - 1 else duration_sec

        # First pass: symmetric expansion clamped to neighbors
        new_start = max(start - half, lo)
        new_end = min(end + half, hi)

        # Redistribute unused backward margin to the forward side
        used_back = start - new_start
        leftover_back = half - used_back
        if leftover_back > 0.0:
            new_end = min(new_end + leftover_back, hi)

        # Redistribute unused forward margin to the backward side
        used_fwd = new_end - end
        leftover_fwd = half - used_fwd
        if leftover_fwd > 0.0:
            new_start = max(new_start - leftover_fwd, lo)

        logging.debug(
            "Caption expanded [%.3f-%.3f] -> [%.3f-%.3f]: %r",
            start,
            end,
            new_start,
            new_end,
            cap.get("text", "")[:40],
        )
        result.append(
            {
                "start": round(new_start, 3),
                "end": round(new_end, 3),
                "text": cap["text"],
            }
        )
    return result


def collect_captions(
    morpheme_times: List[Tuple[float, float, str]],
    keep_intervals: List[dict],
    max_duration: float = 4.0,
    max_bunsetu: int = 12,
    min_bunsetu: int = 3,
    min_duration: float = 1.5,
    silence_flush: float = 1.5,
    duration_sec: float = math.inf,
    bunsetu_separator: str = " ",
) -> List[dict]:
    keep_ranges = [
        (float(iv["start"]), float(iv["end"]))
        for iv in keep_intervals
        if float(iv["end"]) > float(iv["start"])
    ]

    def overlaps_keep(start: float, end: float) -> bool:
        for iv_start, iv_end in keep_ranges:
            if start < iv_end and end > iv_start:
                return True
        return False

    captions: List[dict] = []

    chunk: List[str] = []
    chunk_start = 0.0
    chunk_end = 0.0
    chunk_overlaps_keep = False

    def flush_chunk() -> None:
        if not chunk:
            return
        text = bunsetu_separator.join(chunk)
        logging.debug("Caption chunk [%.3f-%.3f]: %r", chunk_start, chunk_end, text)
        captions.append(
            {
                "start": round(chunk_start, 3),
                "end": round(chunk_end, 3),
                "text": text,
            }
        )

    for m_start, m_end, morpheme in morpheme_times:
        current_overlaps_keep = overlaps_keep(m_start, m_end)
        if chunk:
            speech_duration = chunk_end - chunk_start
            silence_gap = m_start - chunk_end
            crossed_keep_boundary = current_overlaps_keep != chunk_overlaps_keep

            size_limit_reached = (
                speech_duration > max_duration or len(chunk) >= max_bunsetu
            )
            flush_allowed = (
                len(chunk) >= min_bunsetu and speech_duration >= min_duration
            )
            should_flush = (
                (size_limit_reached and flush_allowed)
                or silence_gap > silence_flush
                or crossed_keep_boundary
            )

            if should_flush:
                flush_chunk()
                chunk = []

        if not chunk:
            chunk_start = m_start
            chunk_overlaps_keep = current_overlaps_keep
        chunk.append(morpheme)
        chunk_end = m_end

    if chunk:
        flush_chunk()

    return expand_short_captions(captions, min_duration, duration_sec)
