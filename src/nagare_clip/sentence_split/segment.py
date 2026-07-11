"""Pure re-segmentation core for the sentence_split stage.

No I/O, no LLM, no GiNZA: given a window's words, the window's bunsetsu spans,
and the LLM's bunsetsu-index sentence ranges, rebuild the segment list by
slicing the original words at bunsetsu boundaries.  Words are only reassigned,
never edited, so timing is preserved and text is verbatim by construction.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def iter_windows(segments: list[Any], window_segments: int) -> Iterator[tuple[int, list[Any]]]:
    """Yield (base_index, window) for contiguous whole-segment windows."""
    step = max(1, int(window_segments))
    for base in range(0, len(segments), step):
        yield base, segments[base : base + step]


def window_text_and_words(window: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Concatenate a window's segment words into (text, words)."""
    words: list[dict[str, Any]] = []
    for seg in window:
        words.extend(seg.get("words", []))
    text = "".join(str(w.get("word", "")) for w in words)
    return text, words


def char_to_word_index(words: list[dict[str, Any]]) -> list[int]:
    """Map each character position of the concatenated text to its word index."""
    mapping: list[int] = []
    for wi, w in enumerate(words):
        mapping.extend([wi] * len(str(w.get("word", ""))))
    return mapping


def segment_from_words(words: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a WhisperX-shaped segment dict from a slice of words."""
    text = "".join(str(w.get("word", "")) for w in words)
    starts = [w["start"] for w in words if "start" in w]
    ends = [w["end"] for w in words if "end" in w]
    seg: dict[str, Any] = {}
    if starts:
        seg["start"] = min(starts)
    if ends:
        seg["end"] = max(ends)
    seg["text"] = text
    seg["words"] = words
    return seg


def rebuild_window_segments(
    words: list[dict[str, Any]],
    bunsetsu: list[tuple[int, int, str]],
    ranges: list[tuple[int, int]],
    char2word: list[int],
) -> list[dict[str, Any]]:
    """Rebuild segments from bunsetsu-index sentence ``ranges``.

    Each sentence boundary is the start char of its first bunsetsu, snapped to a
    whole-word boundary via ``char2word`` (never splitting a word).  The first
    sentence always starts at word 0 and the last ends at the final word, so the
    union of word slices is the whole window (verbatim by construction).
    """
    n_words = len(words)
    wbounds: list[int] = [0]
    for a, _ in ranges[1:]:
        c = bunsetsu[a][0]
        wbounds.append(char2word[c] if 0 <= c < len(char2word) else n_words)
    wbounds.append(n_words)
    # Enforce non-decreasing boundaries.
    for i in range(1, len(wbounds)):
        if wbounds[i] < wbounds[i - 1]:
            wbounds[i] = wbounds[i - 1]
    segments: list[dict[str, Any]] = []
    for i in range(len(wbounds) - 1):
        w0, w1 = wbounds[i], wbounds[i + 1]
        if w1 <= w0:
            continue
        segments.append(segment_from_words(words[w0:w1]))
    return segments


def concat_word_text(segments: list[dict[str, Any]]) -> str:
    """Concatenate every word field across segments (verbatim-invariant key)."""
    return "".join(str(w.get("word", "")) for seg in segments for w in seg.get("words", []))


def split_segment_at_silences(
    seg: dict[str, Any], silences: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """Split ``seg`` at word gaps that a silence midpoint corroborates.

    ``silences`` are already-qualifying ``(start, end)`` spans (threshold
    filtering happens upstream). The split point for a silence is the first word
    whose effective start time is ``>=`` the silence midpoint, and only when the
    previous word's effective end is ``<=`` that midpoint — i.e. a genuine
    inter-word gap brackets the midpoint. WhisperX often stretches one word
    across an entire pause; the midpoint then falls inside that word and which
    sentence the word belongs to is undecidable from timing, so such a silence
    is skipped rather than split one word too late. A word without a ``start``
    (``end``) inherits the last known time, so a split never lands between a
    timed word and a following untimed one. Boundaries at word 0 or the segment
    end are dropped, so a silence outside the word range is a no-op. Words are
    only reassigned (via :func:`segment_from_words`), never edited.
    """
    words = seg.get("words", [])
    if not silences or len(words) < 2:
        return [seg]

    eff_start: list[float] = []
    last = float("-inf")
    for w in words:
        if "start" in w:
            last = float(w["start"])
        eff_start.append(last)
    eff_end: list[float] = []
    last = float("-inf")
    for w in words:
        if "end" in w:
            last = float(w["end"])
        eff_end.append(last)

    bounds: set[int] = set()
    for start, end in silences:
        mid = (start + end) / 2.0
        for i, t in enumerate(eff_start):
            if t >= mid:
                if 0 < i < len(words) and eff_end[i - 1] <= mid:
                    bounds.add(i)
                break

    if not bounds:
        return [seg]

    cuts = sorted(bounds)
    pieces: list[dict[str, Any]] = []
    prev = 0
    for c in [*cuts, len(words)]:
        pieces.append(segment_from_words(words[prev:c]))
        prev = c
    return pieces
