"""Haas tokens -> WhisperX-shaped transcript JSON (pure).

One segment per VAD region; ``words`` has one entry per non-whitespace character because the
intervals stage indexes ``words`` by character position.
"""


def _region_index(start: float, regions: list[tuple[float, float]]) -> int:
    best, best_dist = 0, None
    for i, (rs, re_) in enumerate(regions):
        dist = 0.0 if rs <= start <= re_ else min(abs(start - rs), abs(start - re_))
        if best_dist is None or dist < best_dist:  # strict: ties keep the earlier
            best, best_dist = i, dist
    return best


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
