"""Consumer-side rendering of described gaps (summary + director).

Pure: no I/O, no LLM.  Lives here (not in the consumers) so summary and
director share one anchoring rule and one annotation format, and so neither
stage has to import the other.
"""

from __future__ import annotations

from nagare_clip.gap_context.gaps import Gap

_EPS = 0.01
_INDENT = "    "


def anchor_gaps(
    gaps: list[Gap],
    seg_times: list[tuple[float | None, float | None]],
    lines: tuple[int, int] | None = None,
) -> list[tuple[int, Gap]]:
    """Attach each gap to the 1-based line it follows (``0`` = before line 1).

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
        anchor = 0
        for i, (_start, end) in enumerate(seg_times):
            if end is not None and end <= gap.start + _EPS:
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


def annotate_numbered_transcript(transcript: str, anchored: list[tuple[int, Gap]]) -> str:
    """Insert indented ``[silent gap …]`` lines into a numbered transcript.

    Annotation lines are deliberately un-numbered so the director's op line
    references stay unambiguous.  An out-of-range anchor is ignored.  An empty
    *anchored* returns *transcript* unchanged (byte-identical).
    """
    if not anchored:
        return transcript
    lines = transcript.split("\n")
    by_anchor: dict[int, list[Gap]] = {}
    for anchor, gap in anchored:
        if 0 <= anchor <= len(lines):
            by_anchor.setdefault(anchor, []).append(gap)
    out: list[str] = []
    for gap in by_anchor.get(0, []):
        out.append(f"{_INDENT}[silent gap {gap.duration:.1f}s: {gap.description}]")
    for i, line in enumerate(lines):
        out.append(line)
        for gap in by_anchor.get(i + 1, []):
            out.append(f"{_INDENT}[silent gap {gap.duration:.1f}s: {gap.description}]")
    return "\n".join(out)
