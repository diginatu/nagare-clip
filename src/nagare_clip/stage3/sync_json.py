"""Sync corrected text back into WhisperX JSON structure."""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Tuple

from nagare_clip.stage2.llm_filter import PATCH_RE, apply_patches_to_lines

logger = logging.getLogger(__name__)

# Region kind constants
_KEEP = "keep"
_PATCH = "patch"

# Type alias: (kind, orig_start, orig_end, new_text)
Region = Tuple[str, int, int, str]


def _decompose_edit_line(
    edit_line: str, original_text: str
) -> Optional[List[Region]]:
    """Decompose an edit line with ``{{old->new}}`` markers into regions.

    Returns a list of ``(kind, orig_start, orig_end, new_text)`` tuples, or
    ``None`` if the line contains no markers or validation fails.

    - ``"keep"`` regions: text that appears literally in both edit line and
      original; ``new_text`` equals ``original_text[orig_start:orig_end]``.
    - ``"patch"`` regions: ``{{old->new}}`` markers; ``orig_start:orig_end``
      spans the *old* text in the original, ``new_text`` is the replacement.
    """
    markers = list(PATCH_RE.finditer(edit_line))
    if not markers:
        return None

    regions: List[Region] = []
    edit_pos = 0
    orig_pos = 0

    for m in markers:
        # Text before this marker is a keep region
        prefix = edit_line[edit_pos:m.start()]
        if prefix:
            orig_end = orig_pos + len(prefix)
            if original_text[orig_pos:orig_end] != prefix:
                logger.debug(
                    "Keep region mismatch: expected %r got %r",
                    original_text[orig_pos:orig_end],
                    prefix,
                )
                return None
            regions.append((_KEEP, orig_pos, orig_end, prefix))
            orig_pos = orig_end

        old = m.group(1)
        new = m.group(2)

        # Validate old text matches original at current position
        orig_end = orig_pos + len(old)
        if original_text[orig_pos:orig_end] != old:
            logger.debug(
                "Patch old mismatch: expected %r at pos %d, got %r",
                old,
                orig_pos,
                original_text[orig_pos:orig_end],
            )
            return None
        regions.append((_PATCH, orig_pos, orig_end, new))
        orig_pos = orig_end
        edit_pos = m.end()

    # Trailing text after last marker
    trailing = edit_line[edit_pos:]
    if trailing:
        orig_end = orig_pos + len(trailing)
        if original_text[orig_pos:orig_end] != trailing:
            logger.debug(
                "Trailing keep mismatch: expected %r got %r",
                original_text[orig_pos:orig_end],
                trailing,
            )
            return None
        regions.append((_KEEP, orig_pos, orig_end, trailing))
        orig_pos = orig_end

    # Final validation: we should have consumed all of original_text
    if orig_pos != len(original_text):
        logger.debug(
            "Decomposition did not consume full original: %d/%d chars",
            orig_pos,
            len(original_text),
        )
        return None

    return regions


def _word_time_span(
    words: List[Dict[str, Any]],
) -> Optional[Tuple[float, float]]:
    """Return (start, end) time span across *words*, or None if no timing."""
    starts = [w["start"] for w in words if "start" in w]
    ends = [w["end"] for w in words if "end" in w]
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _redistribute_timing(
    original_words: List[Dict[str, Any]],
    new_text: str,
    seg_start: float,
    seg_end: float,
) -> List[Dict[str, Any]]:
    """Linearly redistribute character timing across new_text within [seg_start, seg_end]."""
    if not new_text:
        return []

    duration = seg_end - seg_start
    scores = [w.get("score", 0.0) for w in original_words if "score" in w]
    avg_score = sum(scores) / len(scores) if scores else 0.0

    num_chars = len(new_text)
    return [
        {
            "word": char,
            "start": round(seg_start + (duration * ci / num_chars), 3),
            "end": round(seg_start + (duration * (ci + 1) / num_chars), 3),
            "score": round(avg_score, 4),
        }
        for ci, char in enumerate(new_text)
    ]


def _sync_segment_with_regions(
    original_words: List[Dict[str, Any]],
    regions: List[Region],
) -> List[Dict[str, Any]]:
    """Build new word list using fine-grained regions."""
    new_words: List[Dict[str, Any]] = []

    for kind, orig_start, orig_end, new_text in regions:
        region_words = original_words[orig_start:orig_end]

        if kind == _KEEP:
            new_words.extend(region_words)
            continue

        # Patch region
        if not new_text:
            # Deletion — emit nothing
            continue

        if not region_words:
            # Insertion — use boundary timestamp from neighbours
            if new_words:
                boundary = new_words[-1]["end"]
            elif orig_end < len(original_words):
                boundary = original_words[orig_end].get("start", 0.0)
            else:
                boundary = 0.0
            new_words.extend(
                _redistribute_timing([], new_text, boundary, boundary)
            )
            continue

        span = _word_time_span(region_words)
        if span:
            new_words.extend(
                _redistribute_timing(region_words, new_text, span[0], span[1])
            )

    return new_words


def sync_text_to_json(
    json_data: Dict[str, Any],
    edit_lines: List[str],
) -> Dict[str, Any]:
    """Update WhisperX JSON segments using ``{{old->new}}`` edit lines.

    Each edit line corresponds to a segment in ``json_data["segments"]``.
    Corrected text is derived by applying patches from the edit lines.

    All changes must use ``{{old->new}}`` marker syntax.  Lines without
    markers are treated as unchanged.  A ``ValueError`` is raised if the
    corrected text differs from the original but no markers are present.

    Returns a new dict (deep copy).
    """
    corrected_lines = apply_patches_to_lines(edit_lines)

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
        original_words = segment.get("words", [])

        regions = _decompose_edit_line(edit_lines[i].strip(), original_text)
        if regions is None:
            raise ValueError(
                f"Segment {i}: text changed without {{{{old->new}}}} markers; "
                f"use patch syntax in _edits.txt to indicate changes"
            )
        if original_words:
            segment["words"] = _sync_segment_with_regions(original_words, regions)

    # Rebuild top-level word_segments from all segments' words
    all_words = []
    for segment in segments:
        all_words.extend(segment.get("words", []))
    result["word_segments"] = all_words

    return result
