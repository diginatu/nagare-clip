"""Sync corrected text back into WhisperX JSON structure."""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from nagare_clip.edit_lines import gap_spans, parse_edit_lines
from nagare_clip.text_filter.llm_filter import PATCH_RE, apply_patches_to_lines

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
# the blender stage (Blender VSE). Unlike <keep>, <speed> does NOT force-preserve audio —
# its span is only emitted in speed_ranges; nest inside <keep> to also keep it.
SPEED_TAG_RE = re.compile(r'<speed\s+factor="[0-9.]+">|</speed>')
_SPEED_SPLIT_RE = re.compile(r'(<speed\s+factor="[0-9.]+">|</speed>)')
_SPEED_OPEN_RE = re.compile(r'<speed\s+factor="([0-9.]+)">')

# <overlay text="..." duration="N.N"/> point markers: place a Blender VSE TEXT
# strip starting where the marker sits, for `duration` seconds *of the edited
# timeline*.  The marker has no closing tag on purpose — an end time derived
# from a tag position turned an overlay's on-screen time into a side effect of
# where that tag happened to land (a single-line director op once produced a
# 75-second caption).  Like <speed> (and unlike <keep>), an overlay does NOT
# affect audio retention; if the anchor word is cut, the overlay is skipped in
# the blender stage.  Attribute order is fixed: text, then duration.
OVERLAY_TAG_RE = re.compile(r'<overlay\s+text="[^"]*"\s+duration="[0-9.]+"\s*/>')
_OVERLAY_SPLIT_RE = re.compile(r'(<overlay\s+text="[^"]*"\s+duration="[0-9.]+"\s*/>)')
_OVERLAY_MARK_RE = re.compile(r'<overlay\s+text="([^"]*)"\s+duration="([0-9.]+)"\s*/>')

# A caption may span several on-screen lines, but the marker carrying it must
# stay on ONE _edits.txt line — that file maps line N to WhisperX segment N, so
# a raw newline inside a marker shifts every later line off its segment and the
# whole file is rejected by sync_text_to_json.  Line breaks therefore travel
# escaped as the two characters \ and n, decoded back here.  Only \\ and \n are
# recognised; any other backslash sequence is left verbatim, so a caption
# containing a stray backslash is not silently mangled.
_OVERLAY_ESCAPE_RE = re.compile(r"\\(.)")
_OVERLAY_UNESCAPE = {"n": "\n", "\\": "\\"}


def escape_overlay_text(text: str) -> str:
    """Encode caption *text* for the single-line ``<overlay/>`` attribute."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def unescape_overlay_text(text: str) -> str:
    """Decode an ``<overlay/>`` attribute back into display caption text."""
    return _OVERLAY_ESCAPE_RE.sub(
        lambda m: _OVERLAY_UNESCAPE.get(m.group(1), m.group(0)),
        text,
    )


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
Region = tuple[str, int, int, str]

#: ``{n: (start, end)}`` of the silence after speech line ``n`` —
#: :func:`nagare_clip.edit_lines.gap_spans` of the ORIGINAL (pre-sync) JSON,
#: so a ``<cut>`` that deletes line ``n`` does not move it.
Silences = Mapping[int, tuple[float, float]]


def _decompose_edit_line(edit_line: str, original_text: str) -> list[Region] | None:
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

    regions: list[Region] = []
    edit_pos = 0
    orig_pos = 0

    for m in markers:
        # Text before this marker is a keep region
        prefix = edit_line[edit_pos : m.start()]
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


def _expand_cut_tags(edit_lines: list[str]) -> list[str]:
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
    result: list[str] = []
    cut_open = False
    for line in edit_lines:
        out: list[str] = []
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
                    # Keep edge whitespace outside the patch: the old side of
                    # {{old->}} must match the original segment text, which is
                    # compared stripped at the line level.
                    core = original.strip()
                    if core:
                        lead = original[: len(original) - len(original.lstrip())]
                        trail = original[len(original.rstrip()) :]
                        out.append(lead + "{{" + core + "->}}" + trail)
                    else:
                        out.append(original)
                else:
                    out.append(part)
        result.append("".join(out))
    if cut_open:
        logger.warning("Unclosed <cut>; ignoring")
    return result


def _word_time_span(
    words: list[dict[str, Any]],
) -> tuple[float, float] | None:
    """Return (start, end) time span across *words*, or None if no timing."""
    starts = [w["start"] for w in words if "start" in w]
    ends = [w["end"] for w in words if "end" in w]
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _redistribute_timing(
    original_words: list[dict[str, Any]],
    new_text: str,
    seg_start: float,
    seg_end: float,
) -> list[dict[str, Any]]:
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
    original_words: list[dict[str, Any]],
    regions: list[Region],
) -> list[dict[str, Any]]:
    """Build new word list using fine-grained regions."""
    new_words: list[dict[str, Any]] = []

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
            new_words.extend(_redistribute_timing([], new_text, boundary, boundary))
            continue

        span = _word_time_span(region_words)
        if span:
            new_words.extend(_redistribute_timing(region_words, new_text, span[0], span[1]))

    return new_words


def sync_text_to_json(
    json_data: dict[str, Any],
    edit_lines: list[str],
) -> dict[str, Any]:
    """Update WhisperX JSON segments using ``{{old->new}}`` edit lines.

    Each edit line corresponds to a segment in ``json_data["segments"]``.
    Corrected text is derived by applying patches from the edit lines.

    All changes must use ``{{old->new}}`` marker syntax.  Lines without
    markers are treated as unchanged.  A ``ValueError`` is raised if the
    corrected text differs from the original but no markers are present.

    Any `<keep>...</keep>`, `<speed factor="N.N">...</speed>`, and
    `<overlay text="..." duration="N.N"/>` markers are stripped from each edit
    line before patches are applied; the wrapped text and inner
    `{{old->new}}` markers are otherwise unaffected.  See
    :func:`extract_keep_ranges`, :func:`extract_speed_ranges`, and
    :func:`extract_overlay_marks` for the time extraction passes.

    Silence lines (:mod:`nagare_clip.edit_lines`) carry no words: their
    markers are folded onto the neighbouring speech lines first
    (:meth:`~nagare_clip.edit_lines.EditFile.speech_projection`), so a
    ``<cut>`` opened on the silence after line ``n`` deletes from line ``n+1``.

    Returns a new dict (deep copy).
    """
    edit_lines = parse_edit_lines(edit_lines).speech_projection()
    expanded_lines = _expand_cut_tags(edit_lines)
    cleaned_lines = [
        OVERLAY_TAG_RE.sub("", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line)))
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
                f"use patch syntax in _edits.txt to indicate changes\n"
                f"  original:  {original_text!r}\n"
                f"  corrected: {corrected!r}"
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
    cleaned = OVERLAY_TAG_RE.sub("", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", cleaned)))
    patched = PATCH_RE.sub(lambda m: m.group(2), cleaned)
    return sum(1 for ch in patched if not ch.isspace())


def _first_word_at_or_after(
    segments: list[dict[str, Any]], seg_idx: int, pos: int
) -> dict[str, Any] | None:
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
    segments: list[dict[str, Any]], seg_idx: int, pos: int
) -> dict[str, Any] | None:
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
    segments: list[dict[str, Any]],
    start_anchor: tuple[int, int],
    end_anchor: tuple[int, int],
) -> tuple[float, float] | None:
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


@dataclass(frozen=True)
class _Anchor:
    """Where a tag sits: a word position on a speech line, or a silence line.

    *word* is ``(segment_index, position)``; *silence* the silence's
    ``(start, end)`` — an opener there resolves to its start and a closer to
    its end, wherever on the line the tag sits.  Both ``None``: a silence line
    with no silence behind it, which resolves to nothing.
    """

    word: tuple[int, int] | None = None
    silence: tuple[float, float] | None = None
    on_silence: bool = False


def _anchored_parts(
    edit_lines: list[str],
    segments: list[dict[str, Any]],
    split_re: re.Pattern[str],
    silences: Silences,
):
    """``(part, anchor)`` for every token of the file, in file order.

    A speech line is split as the extractors always split it, each part
    anchored at the word position before it.  A silence line contributes only
    the markers around its opaque body, all anchored at the silence.  Stops at
    a speech line past the JSON's segments, as the extractors always did.
    """
    for slot in parse_edit_lines(edit_lines).slots:
        if slot.is_silence:
            anchor = _Anchor(silence=silences.get(slot.speech_line), on_silence=True)
            for part in split_re.split(slot.markers):
                if part:
                    yield part, anchor
            continue
        seg_idx = slot.speech_line - 1
        if seg_idx >= len(segments):
            return
        output_pos = 0
        for part in split_re.split(slot.text):
            yield part, _Anchor(word=(seg_idx, output_pos))
            if not split_re.fullmatch(part or " "):
                output_pos += _patched_visible_length(part)


def _anchor_start(segments: list[dict[str, Any]], anchor: _Anchor) -> float | None:
    if anchor.on_silence:
        return None if anchor.silence is None else anchor.silence[0]
    word = _first_word_at_or_after(segments, *anchor.word) if anchor.word else None
    return float(word["start"]) if word is not None and "start" in word else None


def _anchor_end(segments: list[dict[str, Any]], anchor: _Anchor) -> float | None:
    if anchor.on_silence:
        return None if anchor.silence is None else anchor.silence[1]
    word = _last_word_before(segments, *anchor.word) if anchor.word else None
    return float(word["end"]) if word is not None and "end" in word else None


def _resolve_anchors(
    segments: list[dict[str, Any]], start: _Anchor, end: _Anchor
) -> tuple[float, float] | None:
    """A span's ``(start, end)`` seconds; ``None`` when empty or unresolvable."""
    if not start.on_silence and not end.on_silence:
        assert start.word is not None and end.word is not None
        return _resolve_keep_range(segments, start.word, end.word)
    start_t = _anchor_start(segments, start)
    end_t = _anchor_end(segments, end)
    if start_t is None or end_t is None or end_t <= start_t:
        return None
    return (start_t, end_t)


def _silences_for(synced_json: dict[str, Any], silences: Silences | None) -> Silences:
    return gap_spans(synced_json) if silences is None else silences


def extract_keep_ranges(
    edit_lines: list[str],
    synced_json: dict[str, Any],
    *,
    silences: Silences | None = None,
) -> list[tuple[float, float]]:
    """Extract force-keep time ranges from `<keep>...</keep>` blocks.

    `<keep>` may be opened on one edit line and closed on a later one; the
    resolved range spans from the first wrapped word's start to the last
    wrapped word's end, with the in-between inter-segment silences falling
    inside.  Positions are tracked in the post-patch, whitespace-stripped
    character stream of each segment.  A tag on a silence line resolves to
    that silence's edge instead: an opener to its start, a closer to its end
    (*silences*; ``None`` derives them from *synced_json*).

    Empty / unclosed (at EOF) / unmatched / nested / invalid-resolved tags
    are skipped with a warning; they do not raise.
    """
    segments = synced_json.get("segments", [])
    silences = _silences_for(synced_json, silences)
    ranges: list[tuple[float, float]] = []
    # `keep_start = None` means no `<keep>` is currently open.
    keep_start: _Anchor | None = None

    for part, anchor in _anchored_parts(edit_lines, segments, _KEEP_SPLIT_RE, silences):
        if part == "<keep>":
            if keep_start is not None:
                logger.warning("Nested <keep> opener; ignoring inner tag")
                continue
            keep_start = anchor
        elif part == "</keep>":
            if keep_start is None:
                logger.warning("Unmatched </keep>; ignoring")
                continue
            resolved = _resolve_anchors(segments, keep_start, anchor)
            keep_start = None
            if resolved is None:
                logger.warning("<keep> resolved to an empty/invalid range; ignoring")
                continue
            ranges.append(resolved)

    if keep_start is not None:
        logger.warning("Unclosed <keep>; ignoring")

    return ranges


def extract_speed_ranges(
    edit_lines: list[str],
    synced_json: dict[str, Any],
    *,
    silences: Silences | None = None,
) -> list[tuple[float, float, float]]:
    """Extract `(start, end, factor)` triples from `<speed factor="N.N">...</speed>` blocks.

    Behaves like :func:`extract_keep_ranges` for span resolution (multi-line
    spans, position tracking, silence lines, error handling) but additionally
    returns the speed factor parsed from each opening tag.
    """
    segments = synced_json.get("segments", [])
    silences = _silences_for(synced_json, silences)
    ranges: list[tuple[float, float, float]] = []
    # `speed_start = None` means no `<speed>` is currently open.
    speed_start: _Anchor | None = None
    speed_factor: float | None = None

    for part, anchor in _anchored_parts(edit_lines, segments, _SPEED_SPLIT_RE, silences):
        open_match = _SPEED_OPEN_RE.fullmatch(part) if part else None
        if open_match is not None:
            if speed_start is not None:
                logger.warning("Nested <speed> opener; ignoring inner tag")
                continue
            speed_start = anchor
            speed_factor = float(open_match.group(1))
        elif part == "</speed>":
            if speed_start is None:
                logger.warning("Unmatched </speed>; ignoring")
                continue
            resolved = _resolve_anchors(segments, speed_start, anchor)
            factor = speed_factor
            speed_start = None
            speed_factor = None
            if resolved is None or factor is None:
                logger.warning("<speed> resolved to an empty/invalid range; ignoring")
                continue
            start_t, end_t = resolved
            ranges.append((start_t, end_t, factor))

    if speed_start is not None:
        logger.warning("Unclosed <speed>; ignoring")

    return ranges


def extract_cut_silences(edit_lines: list[str], silences: Silences) -> list[tuple[float, float]]:
    """The silences a `<cut>` span covers, as ``(start, end)`` excludes.

    A `<cut>` deletes words (:func:`sync_text_to_json`); a silence has none, so
    the silence lines it covers are returned here for the intervals stage to
    drop outright — whatever ``silence_threshold`` says.  A silence line is
    covered when a cut is open as it begins or any cut tag sits on it (an
    opener there starts at the silence's start, a closer ends at its end).
    """
    out: list[tuple[float, float]] = []
    cut_open = False
    for slot in parse_edit_lines(edit_lines).slots:
        tags = _CUT_SPLIT_RE.findall(slot.markers)
        if slot.is_silence and (cut_open or tags):
            span = silences.get(slot.speech_line)
            if span is not None:
                out.append(span)
        for tag in tags:
            cut_open = tag == "<cut>"
    return out


def _resolve_point(
    segments: list[dict[str, Any]],
    anchor: tuple[int, int],
) -> float | None:
    """Resolve a `(segment_index, position)` anchor to a single start time.

    The anchor is the start of the first word at or after it (falling through
    to later segments), so a marker sitting at the end of a line starts when
    the next line does.  Past the very last word there is nothing to fall
    through to, so the last word's end time is used instead.  ``None`` when the
    transcript has no usable timing at all.
    """
    word = _first_word_at_or_after(segments, *anchor)
    if word is not None and "start" in word:
        return float(word["start"])
    last = _last_word_before(segments, anchor[0], anchor[1])
    if last is not None and "end" in last:
        return float(last["end"])
    return None


def extract_overlay_marks(
    edit_lines: list[str],
    synced_json: dict[str, Any],
    *,
    silences: Silences | None = None,
) -> list[tuple[float, float, str]]:
    """Extract `(start, duration, text)` triples from `<overlay .../>` point markers.

    Unlike :func:`extract_keep_ranges`/:func:`extract_speed_ranges`, an overlay
    is a *point*: its position gives the start time and its ``duration``
    attribute — seconds on the **edited** timeline — gives how long the text
    stays on screen (applied by the blender stage, which measures in output
    frames, so cuts and speed ranges inside the window cannot shorten it).
    There is no closing tag and therefore no tag-placement failure mode.  On a
    silence line it starts where that silence starts.

    An empty ``text=""`` or a non-positive ``duration`` is skipped with a
    warning; neither raises.  The returned text is decoded with
    :func:`unescape_overlay_text`, so an escaped ``\\n`` becomes a real line
    break for the blender stage's TEXT strip.
    """
    segments = synced_json.get("segments", [])
    silences = _silences_for(synced_json, silences)
    marks: list[tuple[float, float, str]] = []

    for part, anchor in _anchored_parts(edit_lines, segments, _OVERLAY_SPLIT_RE, silences):
        mark = _OVERLAY_MARK_RE.fullmatch(part) if part else None
        if mark is None:
            continue
        text = unescape_overlay_text(mark.group(1))
        duration = float(mark.group(2))
        if not text:
            logger.warning("<overlay> has empty text; ignoring")
            continue
        if duration <= 0:
            logger.warning("<overlay> duration %.3f is not positive; ignoring", duration)
            continue
        if anchor.on_silence:
            start_t = None if anchor.silence is None else anchor.silence[0]
        else:
            assert anchor.word is not None
            start_t = _resolve_point(segments, anchor.word)
        if start_t is None:
            logger.warning("<overlay> could not be anchored to a word time; ignoring")
            continue
        marks.append((start_t, duration, text))

    return marks
