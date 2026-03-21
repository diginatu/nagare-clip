"""Bunsetsu-level timing from WhisperX character-level data using GiNZA."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    import spacy

CHAR_EPS = 0.02
SILENCE_MAX_WORD_SPAN = 0.6


def build_bunsetu_times(
    whisperx_data: dict,
    nlp: "spacy.language.Language",
    *,
    char_eps: float = CHAR_EPS,
    silence_max_word_span: float = SILENCE_MAX_WORD_SPAN,
) -> List[Tuple[float, float, str]]:
    """Return a flat list of (start, end, surface) for every bunsetsu across all
    segments, sorted by start time.

    Each bunsetsu is a natural Japanese phrase unit produced by
    ``ginza.bunsetu_spans(doc)``, so particles and auxiliaries are already
    attached to the preceding content word.

    end = min(last_char_start + CHAR_EPS, next_bunsetu_start) within a segment,
    so that gap = next.start - this.end reflects real silence only.

    Large intra-bunsetu character gaps (> SILENCE_MAX_WORD_SPAN) caused by
    WhisperX misalignment are detected and the bunsetsu start is snapped
    forward to the later character cluster so silence is not hidden inside
    a single bunsetsu.
    """
    import ginza

    all_bunsetu: List[Tuple[float, float, str]] = []

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

        # Parse with GiNZA and extract bunsetsu spans.
        doc = nlp(seg_text)
        spans = list(ginza.bunsetu_spans(doc))

        # Map each bunsetsu span to (start, tentative_end, surface).
        # Character offsets from the span are used to index char_starts[].
        # When consecutive characters within a bunsetsu have a gap exceeding
        # SILENCE_MAX_WORD_SPAN, WhisperX likely misaligned the earlier
        # character.  The later cluster carries the true timing, so we snap
        # m_start forward to that cluster.
        seg_bunsetu: List[Tuple[float, float, str]] = []
        for span in spans:
            start_char = span.start_char
            end_char = span.end_char  # exclusive

            # Clamp to valid range (seg_text may be shorter than the full doc
            # if the segment is a substring, though normally they are equal).
            start_idx = min(start_char, len(char_starts) - 1)
            last_idx = min(end_char - 1, len(char_starts) - 1)

            m_start = char_starts[start_idx]

            # Scan for large intra-bunsetu gaps and snap to the later cluster.
            for ci in range(start_idx, last_idx):
                if char_starts[ci + 1] - char_starts[ci] > silence_max_word_span:
                    logging.debug(
                        "bunsetu %r: large intra-bunsetu gap %.3fs at char index %d; "
                        "snapping start %.3f -> %.3f",
                        span.text,
                        char_starts[ci + 1] - char_starts[ci],
                        ci,
                        char_starts[start_idx],
                        char_starts[ci + 1],
                    )
                    m_start = char_starts[ci + 1]

            m_end = char_starts[last_idx] + char_eps
            seg_bunsetu.append((m_start, m_end, span.text))

        # Clamp m_end = min(tentative_end, next_bunsetu_start) within segment.
        for i in range(len(seg_bunsetu) - 1):
            m_start, m_end, surface = seg_bunsetu[i]
            next_start = seg_bunsetu[i + 1][0]
            seg_bunsetu[i] = (m_start, min(m_end, next_start), surface)

        all_bunsetu.extend(seg_bunsetu)

    all_bunsetu.sort(key=lambda x: x[0])
    return all_bunsetu


def flatten_bunsetu(whisperx_data: dict) -> List[Tuple[float, float, str]]:
    """Convenience wrapper: loads the ``ja_ginza`` model and calls
    ``build_bunsetu_times``."""
    import spacy

    nlp = spacy.load("ja_ginza")
    return build_bunsetu_times(whisperx_data, nlp)
