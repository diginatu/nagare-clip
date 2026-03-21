"""Sync corrected text back into WhisperX JSON structure."""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def sync_text_to_json(
    json_data: Dict[str, Any], corrected_lines: List[str]
) -> Dict[str, Any]:
    """Update WhisperX JSON segments with corrected text lines.

    Each corrected_line corresponds to a segment in json_data["segments"].
    - Unchanged text: keep everything as-is.
    - Empty text (deletion): set text="", words=[].
    - Changed text: update text, linearly interpolate character timing.

    Returns a new dict (deep copy).
    """
    result = copy.deepcopy(json_data)
    segments = result.get("segments", [])

    for i, segment in enumerate(segments):
        if i >= len(corrected_lines):
            break

        original_text = segment.get("text", "").strip()
        corrected = corrected_lines[i].strip()

        if corrected == original_text:
            continue

        segment["text"] = corrected

        if not corrected:
            # Deleted segment
            segment["words"] = []
            continue

        # Text changed — redistribute character timing
        original_words = segment.get("words", [])
        if not original_words:
            continue

        # Find time span from original words
        starts = [w["start"] for w in original_words if "start" in w]
        ends = [w["end"] for w in original_words if "end" in w]
        if not starts or not ends:
            continue

        seg_start = min(starts)
        seg_end = max(ends)
        duration = seg_end - seg_start

        # Compute average score from originals
        scores = [w.get("score", 0.0) for w in original_words if "score" in w]
        avg_score = sum(scores) / len(scores) if scores else 0.0

        # Build new character-level word entries
        chars = list(corrected)
        num_chars = len(chars)
        new_words = []
        for ci, char in enumerate(chars):
            char_start = seg_start + (duration * ci / num_chars)
            char_end = seg_start + (duration * (ci + 1) / num_chars)
            new_words.append(
                {
                    "word": char,
                    "start": round(char_start, 3),
                    "end": round(char_end, 3),
                    "score": round(avg_score, 4),
                }
            )

        segment["words"] = new_words

    # Rebuild top-level word_segments from all segments' words
    all_words = []
    for segment in segments:
        all_words.extend(segment.get("words", []))
    result["word_segments"] = all_words

    return result
