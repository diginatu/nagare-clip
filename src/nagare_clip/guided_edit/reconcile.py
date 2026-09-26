"""guided_edit reconciliation.

After Pass B2 inserts markers into the verbatim edit lines, the deterministic
reconciler verifies, per director op, that:

1. the underlying transcript text was NOT altered (only markers/patches added) —
   so the small LLM cannot silently rephrase the original; and
2. the op was actually reflected (the expected tag pair / text change appears).

A failing op is reported (and reverted by the caller) so a forgotten closing
tag never corrupts the file.
"""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.edit_lines import EditFile, parse_edit_lines
from nagare_clip.intervals.sync_json import (
    _OVERLAY_MARK_RE,
    _SPEED_OPEN_RE,
    CUT_TAG_RE,
    KEEP_TAG_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
)
from nagare_clip.text_filter.llm_filter import PATCH_RE


def clean_old(line: str) -> str:
    """Underlying original transcript text a line represents.

    Strips all marker tags and resolves every ``{{old->new}}`` back to its
    ``old`` side, so two lines that differ only by added markers/patches yield
    the same value.
    """
    no_tags = CUT_TAG_RE.sub(
        "",
        OVERLAY_TAG_RE.sub("", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line))),
    )
    return PATCH_RE.sub(r"\1", no_tags)


# Per span-op-type (open-tag predicate, close-tag predicate). For a multi-line
# op the open tag must sit on the *first* boundary line and the close tag on the
# *last* — checking mere presence anywhere in the region would let the small LLM
# collapse both tags onto one boundary, silently leaving the rest of the span
# uncut/unprotected.  ``overlay`` is absent on purpose: it is a self-closing
# point marker, checked separately in :func:`_reflection_failure`.
_SPAN_TAGS = {
    "cut": (lambda s: "<cut>" in s, lambda s: "</cut>" in s),
    "keep": (lambda s: "<keep>" in s, lambda s: "</keep>" in s),
    "speed": (
        lambda s: _SPEED_OPEN_RE.search(s) is not None,
        lambda s: "</speed>" in s,
    ),
}


def _reflection_failure(
    before: list[str], after: list[str], markers: list[str], lo: int, hi: int, op: DirectorOp
) -> str | None:
    """*markers* are *after*'s lines as a marker scanner reads them: a silence
    line's opaque body removed, so a description can never pass for a tag."""
    a, b = op.lines
    if op.type == "edit":
        if "".join(after[lo:hi]) == "".join(before[lo:hi]):
            return f"edit op not reflected on lines {a}-{b}"
        return None
    after = markers
    if op.type == "overlay":
        # A point marker, not a span: it must sit on the op's own line.
        if _OVERLAY_MARK_RE.search(after[lo]) is None:
            return f"overlay op: marker missing on line {a}"
        return None
    tags = _SPAN_TAGS.get(op.type)
    if tags is None:
        return f"{op.type} op not reflected on lines {a}-{b}"
    is_open, is_close = tags
    first, last = after[lo], after[hi - 1]
    if not is_open(first):
        return f"{op.type} op: opening tag missing on line {a}"
    if not is_close(last):
        return f"{op.type} op: closing tag missing on line {b}"
    return None


def _silence_structure(parsed: EditFile) -> list[tuple[int, int]]:
    return [(s.file_line, s.speech_line) for s in parsed.silences()]


def _underlying(parsed: EditFile, idx: int) -> str:
    """What an op may not change on physical line ``idx + 1``: a speech line's
    transcript text, or a silence line's bracket."""
    slot = parsed.slots[idx]
    return slot.body if slot.is_silence else clean_old(slot.text)


def verify_op(before: list[str], after: list[str], op: DirectorOp) -> str | None:
    """Return ``None`` if *op* is applied cleanly, else a human-readable reason.

    *before*/*after* are the full edit-line lists around the op's application,
    and *op* addresses their PHYSICAL lines.  Only the op's line range is
    inspected for text; the silence lines are checked everywhere — they are
    generated, and only markers may change around them.
    """
    a, b = op.lines
    lo, hi = a - 1, b  # 0-based slice bounds

    for idx in range(lo, hi):
        if idx >= len(after) or idx >= len(before):
            return f"line {idx + 1} out of range"

    parsed_before, parsed_after = parse_edit_lines(before), parse_edit_lines(after)
    if _silence_structure(parsed_after) != _silence_structure(parsed_before):
        return "silence lines changed (only markers may change around a silence line)"

    for idx in range(lo, hi):
        if _underlying(parsed_after, idx) != _underlying(parsed_before, idx):
            return f"line {idx + 1}: underlying text was altered"

    return _reflection_failure(before, after, parsed_after.marker_text(), lo, hi, op)
