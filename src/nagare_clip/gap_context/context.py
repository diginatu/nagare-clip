"""Consumer-side rendering of described gaps (summary + director).

Pure: no I/O, no LLM.  Lives here (not in the consumers) so summary and
director share one anchoring rule, and so neither
stage has to import the other.
"""

from __future__ import annotations

from nagare_clip.gap_context.gaps import Gap


def anchor_gaps(
    gaps: list[Gap],
    seg_times: list[tuple[float | None, float | None]],
    lines: tuple[int, int] | None = None,
) -> list[tuple[int, Gap]]:
    """Attach each gap to the 1-based line it belongs to (``0`` = before line 1).

    The gap's MIDPOINT picks the line — the last one that starts before it —
    not the gap's start.  Keying on the start broke on WhisperX's habit of
    stretching an utterance's final word across the beginning of a pause: the
    following line then ends *after* the gap starts, fails to qualify, and the
    annotation lands one line early (54% of them, on the real corpus).  Worse,
    ``sentence_split`` deliberately declines to split a silence whose midpoint
    falls inside such a stretched word, so that silence stays *inside* a
    sentence segment — where the line's own bracket already reports it as
    ``Ys silence`` while the annotation was printed above the line.

    The midpoint rule reads as: print the annotation on the line whose own
    bracket accounts for this silence — as a trailing ``gap Zs`` when the
    midpoint falls between two lines, as internal ``Ys silence`` when it falls
    inside one.

    Static gaps (no meaningful on-screen change) are skipped entirely — they
    carry no editorial signal, so neither consumer renders them.

    *seg_times* is always the WHOLE source's, so one anchoring rule serves both
    a whole source and one segment of a split one.  ``lines=(a, b)`` restricts
    the result to the gaps that segment owns and rebases the anchors onto its
    slice: a gap the segment does not contain is dropped rather than rendered
    at its edge, and the gap *preceding* line ``a`` belongs to this segment
    (anchor ``0``) because that is the footage the manifest gives it.  A gap
    after line ``b`` belongs to whatever segment plays next — unless ``b`` is
    the source's last line, where there is no next segment to own it.
    """
    out: list[tuple[int, Gap]] = []
    for gap in gaps:
        if gap.static:
            continue
        midpoint = (gap.start + gap.end) / 2
        anchor = 0
        for i, (start, _end) in enumerate(seg_times):
            if start is not None and start <= midpoint:
                anchor = i + 1
        if lines is not None:
            first, last = lines
            limit = last if last < len(seg_times) else last + 1
            if not (first - 1 <= anchor < limit):
                continue
            anchor -= first - 1
        out.append((anchor, gap))
    return out


def format_gap_block(anchored: list[tuple[int, Gap]]) -> str:
    """The ``## Silent gaps`` block appended to the summary stage's user prompt."""
    if not anchored:
        return ""
    lines = ["## Silent gaps (visual context)"]
    for anchor, gap in anchored:
        where = f"after line {anchor}" if anchor else "before line 1"
        lines.append(
            f"- {where} ({gap.start:.1f}s-{gap.end:.1f}s, {gap.duration:.1f}s): {gap.description}"
        )
    return "\n".join(lines)
