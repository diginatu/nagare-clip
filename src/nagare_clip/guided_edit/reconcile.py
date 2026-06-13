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


def _reflected(region_before: str, region_after: str, op: DirectorOp) -> bool:
    if op.type == "cut":
        return "<cut>" in region_after and "</cut>" in region_after
    if op.type == "keep":
        return "<keep>" in region_after and "</keep>" in region_after
    if op.type == "speed":
        return (
            _SPEED_OPEN_RE.search(region_after) is not None
            and "</speed>" in region_after
        )
    if op.type == "overlay":
        return (
            _OVERLAY_OPEN_RE.search(region_after) is not None
            and "</overlay>" in region_after
        )
    if op.type == "edit":
        return region_after != region_before
    return False


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

    region_before = "".join(before[lo:hi])
    region_after = "".join(after[lo:hi])
    if not _reflected(region_before, region_after, op):
        return f"{op.type} op not reflected on lines {a}-{b}"
    return None
