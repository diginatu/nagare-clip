"""Caption chunking from morpheme-level timing data."""

from __future__ import annotations

import logging
from typing import List, Tuple


def collect_captions(
    morpheme_times: List[Tuple[float, float, str]],
    keep_intervals: List[dict],
    max_duration: float = 4.0,
    max_morphemes: int = 12,
    min_morphemes: int = 3,
    min_duration: float = 1.5,
    silence_flush: float = 1.5,
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
        text = "".join(chunk)
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
                speech_duration > max_duration or len(chunk) >= max_morphemes
            )
            flush_allowed = (
                len(chunk) >= min_morphemes and speech_duration >= min_duration
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

    return captions
