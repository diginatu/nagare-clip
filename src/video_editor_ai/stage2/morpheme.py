"""Morpheme-level timing from WhisperX character-level data."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from fugashi import Tagger

CHAR_EPS = 0.02
SILENCE_MAX_WORD_SPAN = 0.6


def build_morpheme_times(
    whisperx_data: dict,
    tagger: Tagger,
) -> List[Tuple[float, float, str]]:
    """
    Returns a flat list of (start, end, surface) for every morpheme
    across all segments, sorted by start time.

    end = min(last_char_start + CHAR_EPS, next_morpheme_start)
    so that gap = next.start - this.end reflects real silence only.
    """
    all_morphemes: List[Tuple[float, float, str]] = []

    for segment in whisperx_data.get("segments", []):
        seg_text = segment.get("text", "").strip()
        char_entries = segment.get("words", [])
        if not seg_text or not char_entries:
            continue

        # Build char_starts: one start time per character of seg_text,
        # inheriting the last valid start for entries with missing start times.
        char_starts: List[float] = []
        last_valid = float(char_entries[0].get("start") or 0.0)
        for entry in char_entries:
            s = entry.get("start")
            if s is not None:
                last_valid = float(s)
            char_starts.append(last_valid)
        while len(char_starts) < len(seg_text):
            char_starts.append(char_starts[-1] if char_starts else 0.0)
        char_starts = char_starts[: len(seg_text)]

        # Morphological analysis
        morphemes: List[str] = [w.surface for w in tagger(seg_text)]

        # Map each morpheme to (start, tentative_end, surface).
        # tentative_end = last_char_start + eps; will be clamped below.
        # When consecutive characters within a morpheme have a gap
        # exceeding SILENCE_MAX_WORD_SPAN, WhisperX likely misaligned the
        # earlier character.  In observed data the later cluster carries the
        # true timing, so we shift m_start forward to that cluster.
        seg_morphemes: List[Tuple[float, float, str]] = []
        char_cursor = 0
        for morpheme in morphemes:
            m_len = len(morpheme)
            start_idx = min(char_cursor, len(char_starts) - 1)
            last_idx = min(char_cursor + m_len - 1, len(char_starts) - 1)
            m_start = char_starts[start_idx]
            # Scan for large intra-morpheme gaps and snap to the later cluster.
            for ci in range(start_idx, last_idx):
                if char_starts[ci + 1] - char_starts[ci] > SILENCE_MAX_WORD_SPAN:
                    logging.debug(
                        "morpheme %r: large intra-morpheme gap %.3fs at char index %d; "
                        "snapping start %.3f -> %.3f",
                        morpheme,
                        char_starts[ci + 1] - char_starts[ci],
                        ci,
                        char_starts[start_idx],
                        char_starts[ci + 1],
                    )
                    m_start = char_starts[ci + 1]
            m_end = char_starts[last_idx] + CHAR_EPS
            seg_morphemes.append((m_start, m_end, morpheme))
            char_cursor += m_len

        # Apply min(tentative_end, next_morpheme_start) within segment
        for i in range(len(seg_morphemes) - 1):
            m_start, m_end, surface = seg_morphemes[i]
            next_start = seg_morphemes[i + 1][0]
            seg_morphemes[i] = (m_start, min(m_end, next_start), surface)

        all_morphemes.extend(seg_morphemes)

    all_morphemes.sort(key=lambda x: x[0])
    return all_morphemes


def flatten_words(whisperx_data: dict) -> List[Tuple[float, float, str]]:
    from fugashi import Tagger as _Tagger

    tagger = _Tagger("-Owakati")
    return build_morpheme_times(whisperx_data, tagger)
