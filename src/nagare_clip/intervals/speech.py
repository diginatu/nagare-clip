"""Speech span extraction and duration computation from WhisperX data."""

from __future__ import annotations

from collections.abc import Sequence

from nagare_clip.intervals.bunsetu import CHAR_EPS, SILENCE_MAX_WORD_SPAN


def build_speech_spans(whisperx_data: dict) -> list[tuple[float, float]]:
    """Build speech spans from WhisperX word timings for silence detection."""
    spans: list[tuple[float, float]] = []

    for segment in whisperx_data.get("segments", []):
        raw_entries = segment.get("words", [])
        entries = [e for e in raw_entries if e.get("start") is not None]
        for idx, entry in enumerate(entries):
            end_raw = entry.get("end")
            start = float(entry["start"])

            next_start = None
            if idx + 1 < len(entries):
                next_start = float(entries[idx + 1].get("start"))

            if end_raw is None:
                end = start + SILENCE_MAX_WORD_SPAN
            else:
                end = float(end_raw)

            end = min(end, start + SILENCE_MAX_WORD_SPAN)
            if next_start is not None:
                end = min(end, next_start)

            if end <= start:
                end = start + CHAR_EPS

            spans.append((start, end))

    spans.sort(key=lambda x: x[0])
    return spans


def line_speech_spans(whisperx_data: dict) -> list[list[tuple[float, float]]]:
    """One :func:`build_speech_spans` list per WhisperX segment (= per line).

    ``build_speech_spans`` only ever looks at the next word *within* a segment,
    so running it per segment yields exactly the spans the whole-file call
    produces for that segment.  The space between line ``n``'s last span and
    line ``n+1``'s first is therefore the silence the pipeline drops — the one
    definition a marker on a silence line resolves to
    (:func:`nagare_clip.edit_lines.gap_spans`) and
    :mod:`nagare_clip.director.silence_lines` shows the director.
    """
    return [
        build_speech_spans({"segments": [segment]}) for segment in whisperx_data.get("segments", [])
    ]


def get_duration_sec(whisperx_data: dict, words: Sequence[tuple[float, float, str]]) -> float:
    max_end = 0.0
    if words:
        max_end = max(max_end, max(end for _, end, _ in words))

    for segment in whisperx_data.get("segments", []):
        end = segment.get("end")
        if end is not None:
            max_end = max(max_end, float(end))

    if isinstance(whisperx_data.get("duration"), (int, float)):
        max_end = max(max_end, float(whisperx_data["duration"]))

    return max_end
