"""Pure re-segmentation core for the sentence_split stage.

No I/O, no LLM, no GiNZA: given a window's words, the window's bunsetsu spans,
and the LLM's bunsetsu-index sentence ranges, rebuild the segment list by
slicing the original words at bunsetsu boundaries.  Words are only reassigned,
never edited, so timing is preserved and text is verbatim by construction.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple


def iter_windows(
    segments: List[Any], window_segments: int
) -> Iterator[Tuple[int, List[Any]]]:
    """Yield (base_index, window) for contiguous whole-segment windows."""
    step = max(1, int(window_segments))
    for base in range(0, len(segments), step):
        yield base, segments[base : base + step]


def window_text_and_words(window: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Concatenate a window's segment words into (text, words)."""
    words: List[Dict[str, Any]] = []
    for seg in window:
        words.extend(seg.get("words", []))
    text = "".join(str(w.get("word", "")) for w in words)
    return text, words


def char_to_word_index(words: List[Dict[str, Any]]) -> List[int]:
    """Map each character position of the concatenated text to its word index."""
    mapping: List[int] = []
    for wi, w in enumerate(words):
        mapping.extend([wi] * len(str(w.get("word", ""))))
    return mapping


def segment_from_words(words: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a WhisperX-shaped segment dict from a slice of words."""
    text = "".join(str(w.get("word", "")) for w in words)
    starts = [w["start"] for w in words if "start" in w]
    ends = [w["end"] for w in words if "end" in w]
    seg: Dict[str, Any] = {}
    if starts:
        seg["start"] = min(starts)
    if ends:
        seg["end"] = max(ends)
    seg["text"] = text
    seg["words"] = words
    return seg


def rebuild_window_segments(
    words: List[Dict[str, Any]],
    bunsetsu: List[Tuple[int, int, str]],
    ranges: List[Tuple[int, int]],
    char2word: List[int],
) -> List[Dict[str, Any]]:
    """Rebuild segments from bunsetsu-index sentence ``ranges``.

    Each sentence boundary is the start char of its first bunsetsu, snapped to a
    whole-word boundary via ``char2word`` (never splitting a word).  The first
    sentence always starts at word 0 and the last ends at the final word, so the
    union of word slices is the whole window (verbatim by construction).
    """
    n_words = len(words)
    wbounds: List[int] = [0]
    for a, _ in ranges[1:]:
        c = bunsetsu[a][0]
        wbounds.append(char2word[c] if 0 <= c < len(char2word) else n_words)
    wbounds.append(n_words)
    # Enforce non-decreasing boundaries.
    for i in range(1, len(wbounds)):
        if wbounds[i] < wbounds[i - 1]:
            wbounds[i] = wbounds[i - 1]
    segments: List[Dict[str, Any]] = []
    for i in range(len(wbounds) - 1):
        w0, w1 = wbounds[i], wbounds[i + 1]
        if w1 <= w0:
            continue
        segments.append(segment_from_words(words[w0:w1]))
    return segments


def concat_word_text(segments: List[Dict[str, Any]]) -> str:
    """Concatenate every word field across segments (verbatim-invariant key)."""
    return "".join(
        str(w.get("word", "")) for seg in segments for w in seg.get("words", [])
    )
