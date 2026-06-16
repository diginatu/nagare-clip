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

from typing import List, Optional

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.stage2.llm_filter import PATCH_RE
from nagare_clip.stage3.sync_json import (
    CUT_TAG_RE,
    KEEP_TAG_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
    _OVERLAY_OPEN_RE,
    _SPEED_OPEN_RE,
)


def clean_old(line: str) -> str:
    """Underlying original transcript text a line represents.

    Strips all marker tags and resolves every ``{{old->new}}`` back to its
    ``old`` side, so two lines that differ only by added markers/patches yield
    the same value.
    """
    no_tags = CUT_TAG_RE.sub(
        "",
        OVERLAY_TAG_RE.sub(
            "", SPEED_TAG_RE.sub("", KEEP_TAG_RE.sub("", line))
        ),
    )
    return PATCH_RE.sub(r"\1", no_tags)


# Per span-op-type (open-tag predicate, close-tag predicate). For a multi-line
# op the open tag must sit on the *first* boundary line and the close tag on the
# *last* — checking mere presence anywhere in the region would let the small LLM
# collapse both tags onto one boundary, silently leaving the rest of the span
# uncut/unprotected.
_SPAN_TAGS = {
    "cut": (lambda s: "<cut>" in s, lambda s: "</cut>" in s),
    "keep": (lambda s: "<keep>" in s, lambda s: "</keep>" in s),
    "speed": (
        lambda s: _SPEED_OPEN_RE.search(s) is not None,
        lambda s: "</speed>" in s,
    ),
    "overlay": (
        lambda s: _OVERLAY_OPEN_RE.search(s) is not None,
        lambda s: "</overlay>" in s,
    ),
}


def _reflection_failure(
    before: List[str], after: List[str], lo: int, hi: int, op: DirectorOp
) -> Optional[str]:
    a, b = op.lines
    if op.type == "edit":
        if "".join(after[lo:hi]) == "".join(before[lo:hi]):
            return f"edit op not reflected on lines {a}-{b}"
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


def verify_op(
    before: List[str], after: List[str], op: DirectorOp
) -> Optional[str]:
    """Return ``None`` if *op* is applied cleanly, else a human-readable reason.

    *before*/*after* are the full edit-line lists around the op's application.
    Only the op's line range is inspected.
    """
    a, b = op.lines
    lo, hi = a - 1, b  # 0-based slice bounds

    for idx in range(lo, hi):
        if idx >= len(after) or idx >= len(before):
            return f"line {idx + 1} out of range"
        if clean_old(after[idx]) != clean_old(before[idx]):
            return f"line {idx + 1}: underlying text was altered"

    return _reflection_failure(before, after, lo, hi, op)
