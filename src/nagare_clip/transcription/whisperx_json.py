"""Haas tokens -> WhisperX-shaped transcript JSON (pure).

One segment per VAD region; ``words`` has one entry per non-whitespace character because the
intervals stage indexes ``words`` by character position.
"""

from bisect import bisect_left


def _region_index(start: float, regions: list[tuple[float, float]]) -> int:
    """Region holding ``start``; in a gap, the nearer edge; a tie keeps the earlier.

    ``regions`` are sorted and non-overlapping (VAD output), so only the two
    neighbours of ``start`` can win.
    """
    starts = [rs for rs, _ in regions]
    i = bisect_left(starts, start)  # regions[:i] start strictly before ``start``
    if i == 0:
        return 0
    left_dist = max(0.0, start - regions[i - 1][1])
    if i == len(regions):
        return i - 1
    return i - 1 if left_dist <= regions[i][0] - start else i


def _char_words(token: dict) -> list[dict]:
    start, end = token["start"], token["end"]
    # Whitespace never becomes a word; the token's time is split evenly over
    # the remaining (non-whitespace) characters only.
    text = "".join(ch for ch in token["text"] if not ch.isspace())
    n = len(text)
    span = end - start
    return [
        {
            "word": ch,
            "start": round(start + span * i / n, 3),
            "end": round(start + span * (i + 1) / n, 3),
            "score": 1.0,
        }
        for i, ch in enumerate(text)
    ]


def build_whisperx_json(tokens: list[dict], regions: list[tuple[float, float]]) -> dict:
    per_region: dict[int, list[dict]] = {}
    if regions:
        for token in tokens:
            words = _char_words(token)
            if not words:
                continue
            idx = _region_index(token["start"], regions)
            per_region.setdefault(idx, []).extend(words)
    segments = []
    for idx in sorted(per_region):
        words = per_region[idx]
        segments.append(
            {
                "start": words[0]["start"],
                "end": words[-1]["end"],
                "text": "".join(w["word"] for w in words),
                "words": words,
            }
        )
    return {
        "segments": segments,
        "word_segments": [w for s in segments for w in s["words"]],
        "language": "ja",
    }


def transcript_text(data: dict) -> str:
    texts = [s["text"] for s in data["segments"]]
    return "\n".join(texts) + "\n" if texts else ""
