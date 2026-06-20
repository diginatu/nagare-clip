"""Sync corrected text back into WhisperX JSON structure."""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from nagare_clip.stage2.llm_filter import PATCH_RE, apply_patches_to_lines

logger = logging.getLogger(__name__)

# Region kind constants
_KEEP = "keep"
_PATCH = "patch"

# <keep>...</keep> force-keep markers (added by humans after the LLM filter).
# Matched as literal tags; the inner text is preserved (and may itself contain
# {{old->new}} patches).
KEEP_TAG_RE = re.compile(r"</?keep>")
_KEEP_SPLIT_RE = re.compile(r"(<keep>|</keep>)")

# <speed factor="N.N">...</speed> markers: carry a playback speed factor for
# Stage 5 (Blender VSE). Unlike <keep>, <speed> does NOT force-preserve audio —
# its span is only emitted in speed_ranges; nest inside <keep> to also keep it.
SPEED_TAG_RE = re.compile(r'<speed\s+factor="[0-9.]+">|</speed>')
_SPEED_SPLIT_RE = re.compile(r'(<speed\s+factor="[0-9.]+">|</speed>)')
_SPEED_OPEN_RE = re.compile(r'<speed\s+factor="([0-9.]+)">')

# <overlay text="...">...</overlay> markers: place a Blender VSE TEXT strip
# at the wrapped span's time range. Like <speed> (and unlike <keep>), overlay
# does NOT affect audio retention; if the wrapped audio is cut, the overlay is
# skipped in Stage 5.
OVERLAY_TAG_RE = re.compile(r'<overlay\s+text="[^"]*">|</overlay>')
_OVERLAY_SPLIT_RE = re.compile(r'(<overlay\s+text="[^"]*">|</overlay>)')
_OVERLAY_OPEN_RE = re.compile(r'<overlay\s+text="([^"]*)">')

# <cut>...</cut> deletion-shorthand markers (added by humans / guided_edit).
# A <cut> span deletes the wrapped text; it desugars to a {{wrapped->}}
# deletion patch before the normal patch flow, so the existing
# patch/decompose/timing machinery removes those words.  Removing a large
# span then falls out of the timeline via the interval stage's silence-gap
# mechanism.  A <cut> may open on one edit line and close on a later one.
CUT_TAG_RE = re.compile(r"</?cut>")
_CUT_SPLIT_RE = re.compile(r"(<cut>|</cut>)")
_CUT_PAIR_RE = re.compile(r"<cut>.*?</cut>")

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


def _expand_cut_tags(edit_lines: List[str]) -> List[str]:
    """Desugar `<cut>...</cut>` spans into `{{wrapped->}}` deletion patches.

    The wrapped text (with any inner ``{{old->new}}`` markers resolved back to
    their *old* side) becomes the ``old`` of a deletion patch, so the existing
    patch/decompose/timing machinery removes those words.  A `<cut>` may open
    on one line and close on a later one; text on fully-wrapped intermediate
    lines is deleted in whole.

    Unmatched `</cut>`, nested `<cut>`, and an unclosed `<cut>` at EOF are
    ignored with a warning (the offending tag is dropped, surrounding text
    kept); the function never raises.
    """
    result: List[str] = []
    cut_open = False
    for line in edit_lines:
        out: List[str] = []
        for part in _CUT_SPLIT_RE.split(line):
            if part == "<cut>":
                if cut_open:
                    logger.warning("Nested <cut> opener; ignoring inner tag")
                    continue
                cut_open = True
            elif part == "</cut>":
                if not cut_open:
                    logger.warning("Unmatched </cut>; ignoring")
                    continue
                cut_open = False
            elif part:
                if cut_open:
                    original = PATCH_RE.sub(r"\1", part)
                    if original:
                        out.append("{{" + original + "->}}")
                else:
                    out.append(part)
        result.append("".join(out))
    if cut_open:
        logger.warning("Unclosed <cut>; ignoring")
    return result


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

    Any `<keep>...</keep>`, `<speed factor="N.N">...</speed>`, and
    `<overlay text="...">...</overlay>` markers are stripped from each edit
    line before patches are applied; the wrapped text and inner
    `{{old->new}}` markers are otherwise unaffected.  See
    :func:`extract_keep_ranges`, :func:`extract_speed_ranges`, and
    :func:`extract_overlay_ranges` for the time-range extraction passes.

    Returns a new dict (deep copy).
    """
    expanded_lines = _expand_cut_tags(edit_lines)
    cleaned_lines = [
        OVERLAY_TAG_RE.sub(
            "", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line))
        )
        for line in expanded_lines
    ]
    corrected_lines = apply_patches_to_lines(cleaned_lines)

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

        regions = _decompose_edit_line(cleaned_lines[i].strip(), original_text)
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


def _patched_visible_length(text: str) -> int:
    """Length in non-whitespace characters of `text` after applying patches
    and stripping any <keep>/<speed>/<overlay>/<cut> marker tags.

    `<cut>...</cut>` spans are removed *including* their inner text (those
    words are deleted from the synced JSON), so positions of any neighbouring
    keep/speed/overlay tags on the same line stay aligned with the reduced
    word list."""
    cleaned = _CUT_PAIR_RE.sub("", text)
    cleaned = CUT_TAG_RE.sub("", cleaned)
    cleaned = OVERLAY_TAG_RE.sub(
        "", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", cleaned))
    )
    patched = PATCH_RE.sub(lambda m: m.group(2), cleaned)
    return sum(1 for ch in patched if not ch.isspace())


def _first_word_at_or_after(
    segments: List[Dict[str, Any]], seg_idx: int, pos: int
) -> Optional[Dict[str, Any]]:
    """First word at index >= pos in segments[seg_idx]; falls through to the
    next segment's first word when pos is past the current segment's words."""
    while seg_idx < len(segments):
        words = segments[seg_idx].get("words", [])
        if pos < len(words):
            return words[pos]
        seg_idx += 1
        pos = 0
    return None


def _last_word_before(
    segments: List[Dict[str, Any]], seg_idx: int, pos: int
) -> Optional[Dict[str, Any]]:
    """Last word at index < pos in segments[seg_idx]; falls back to the
    previous segment's last word when pos is 0 (or all earlier indices are
    out of range)."""
    while seg_idx >= 0:
        words = segments[seg_idx].get("words", [])
        last_idx = min(pos, len(words)) - 1
        if last_idx >= 0:
            return words[last_idx]
        seg_idx -= 1
        if seg_idx >= 0:
            pos = len(segments[seg_idx].get("words", []))
    return None


def _resolve_keep_range(
    segments: List[Dict[str, Any]],
    start_anchor: Tuple[int, int],
    end_anchor: Tuple[int, int],
) -> Optional[Tuple[float, float]]:
    """Resolve `(segment_index, position)` anchors to `(start_time, end_time)`.

    Returns ``None`` when the resolved range wraps no words, is missing
    timings, or collapses to an empty/inverted interval.
    """
    first_word = _first_word_at_or_after(segments, *start_anchor)
    last_word = _last_word_before(segments, *end_anchor)
    if first_word is None or last_word is None:
        return None
    if "start" not in first_word or "end" not in last_word:
        return None
    start_t = float(first_word["start"])
    end_t = float(last_word["end"])
    if end_t <= start_t:
        return None
    return (start_t, end_t)


def extract_keep_ranges(
    edit_lines: List[str], synced_json: Dict[str, Any]
) -> List[Tuple[float, float]]:
    """Extract force-keep time ranges from `<keep>...</keep>` blocks.

    `<keep>` may be opened on one edit line and closed on a later one; the
    resolved range spans from the first wrapped word's start to the last
    wrapped word's end, with the in-between inter-segment silences falling
    inside.  Positions are tracked in the post-patch, whitespace-stripped
    character stream of each segment.

    Empty / unclosed (at EOF) / unmatched / nested / invalid-resolved tags
    are skipped with a warning; they do not raise.
    """
    segments = synced_json.get("segments", [])
    ranges: List[Tuple[float, float]] = []
    # `keep_start = None` means no `<keep>` is currently open.
    keep_start: Optional[Tuple[int, int]] = None

    for seg_idx, line in enumerate(edit_lines):
        if seg_idx >= len(segments):
            break
        output_pos = 0
        for part in _KEEP_SPLIT_RE.split(line):
            if part == "<keep>":
                if keep_start is not None:
                    logger.warning("Nested <keep> opener; ignoring inner tag")
                    continue
                keep_start = (seg_idx, output_pos)
            elif part == "</keep>":
                if keep_start is None:
                    logger.warning("Unmatched </keep>; ignoring")
                    continue
                resolved = _resolve_keep_range(
                    segments, keep_start, (seg_idx, output_pos)
                )
                keep_start = None
                if resolved is None:
                    logger.warning(
                        "<keep> resolved to an empty/invalid range; ignoring"
                    )
                    continue
                ranges.append(resolved)
            else:
                output_pos += _patched_visible_length(part)

    if keep_start is not None:
        logger.warning("Unclosed <keep>; ignoring")

    return ranges


def extract_speed_ranges(
    edit_lines: List[str], synced_json: Dict[str, Any]
) -> List[Tuple[float, float, float]]:
    """Extract `(start, end, factor)` triples from `<speed factor="N.N">...</speed>` blocks.

    Behaves like :func:`extract_keep_ranges` for span resolution (multi-line
    spans, position tracking, error handling) but additionally returns the
    speed factor parsed from each opening tag.
    """
    segments = synced_json.get("segments", [])
    ranges: List[Tuple[float, float, float]] = []
    # `speed_start = None` means no `<speed>` is currently open.
    speed_start: Optional[Tuple[int, int]] = None
    speed_factor: Optional[float] = None

    for seg_idx, line in enumerate(edit_lines):
        if seg_idx >= len(segments):
            break
        output_pos = 0
        for part in _SPEED_SPLIT_RE.split(line):
            open_match = _SPEED_OPEN_RE.fullmatch(part) if part else None
            if open_match is not None:
                if speed_start is not None:
                    logger.warning("Nested <speed> opener; ignoring inner tag")
                    continue
                speed_start = (seg_idx, output_pos)
                speed_factor = float(open_match.group(1))
            elif part == "</speed>":
                if speed_start is None:
                    logger.warning("Unmatched </speed>; ignoring")
                    continue
                resolved = _resolve_keep_range(
                    segments, speed_start, (seg_idx, output_pos)
                )
                factor = speed_factor
                speed_start = None
                speed_factor = None
                if resolved is None or factor is None:
                    logger.warning(
                        "<speed> resolved to an empty/invalid range; ignoring"
                    )
                    continue
                start_t, end_t = resolved
                ranges.append((start_t, end_t, factor))
            else:
                output_pos += _patched_visible_length(part)

    if speed_start is not None:
        logger.warning("Unclosed <speed>; ignoring")

    return ranges


def extract_overlay_ranges(
    edit_lines: List[str], synced_json: Dict[str, Any]
) -> List[Tuple[float, float, str]]:
    """Extract `(start, end, text)` triples from `<overlay text="...">...</overlay>` blocks.

    Behaves like :func:`extract_speed_ranges` for span resolution (multi-line
    spans, position tracking, error handling) but returns the overlay text
    parsed from each opening tag instead of a numeric factor.  Overlays do
    NOT affect audio retention; they are consumed by Stage 5 to place
    on-screen TEXT strips only.

    Empty ``text=""`` attributes are treated as invalid and skipped with a
    warning (an overlay with no text would have nothing to display).
    """
    segments = synced_json.get("segments", [])
    ranges: List[Tuple[float, float, str]] = []
    overlay_start: Optional[Tuple[int, int]] = None
    overlay_text: Optional[str] = None

    for seg_idx, line in enumerate(edit_lines):
        if seg_idx >= len(segments):
            break
        output_pos = 0
        for part in _OVERLAY_SPLIT_RE.split(line):
            open_match = _OVERLAY_OPEN_RE.fullmatch(part) if part else None
            if open_match is not None:
                if overlay_start is not None:
                    logger.warning(
                        "Nested <overlay> opener; ignoring inner tag"
                    )
                    continue
                overlay_start = (seg_idx, output_pos)
                overlay_text = open_match.group(1)
            elif part == "</overlay>":
                if overlay_start is None:
                    logger.warning("Unmatched </overlay>; ignoring")
                    continue
                resolved = _resolve_keep_range(
                    segments, overlay_start, (seg_idx, output_pos)
                )
                text = overlay_text
                overlay_start = None
                overlay_text = None
                if resolved is None or not text:
                    logger.warning(
                        "<overlay> resolved to an empty/invalid range; ignoring"
                    )
                    continue
                start_t, end_t = resolved
                ranges.append((start_t, end_t, text))
            else:
                output_pos += _patched_visible_length(part)

    if overlay_start is not None:
        logger.warning("Unclosed <overlay>; ignoring")

    return ranges
