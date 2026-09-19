"""Build the cross-video context block the director injects into its prompt.

Lives in the ``director`` package (not ``director_llm``) because it depends on
the ``summary`` and ``plan`` stages; ``director_llm`` stays free of those imports
so ``summary`` can keep importing it without a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from nagare_clip.director.director_llm import DirectorOp, clean_for_display
from nagare_clip.order import Segment, segment_label
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.summary.summarize import ProjectSummary

#: Why the neighbouring lines are in the prompt.  Deliberately a rule and not a
#: worked example: an example in this prompt anchors harder than the instruction
#: around it (improvement 11).
SEAM_NOTE = (
    "Your footage is NOT a standalone episode — it plays inside one longer finished "
    "video, and the lines below are what the viewer hears immediately before and "
    "after it. An opening greeting or a closing sign-off in your footage is "
    "addressing an audience that is already mid-video. They are NOT part of your "
    "transcript and carry no numbering — every op you emit refers to your own "
    "numbered lines only."
)


#: Heads the whole-video reference block (``director.whole_project_context``).
#: A rule, not an example, for the same reason as :data:`SEAM_NOTE`; and short,
#: because every growth of instruction prose here has cost something
#: (improvement 16).  The transcripts below it are data.
WHOLE_VIDEO_NOTE = (
    "The whole finished video, every segment in playback order, for reference. "
    "[k]N is line N of segment k; default runtime is what plays if no op is "
    "applied. Your ops address only the plain-numbered transcript in the user "
    "message — never put a [k]N number in an op."
)

_NUMBERED_LINE_RE = re.compile(r"^(\d+):", re.MULTILINE)


def qualify_line_numbers(transcript: str, index: int) -> str:
    """Prefix every numbered line of a rendered transcript with ``[index]``.

    A bare number in the whole-video reference would be ANOTHER segment's
    coordinate, and the parser accepts any number inside this segment's own
    range — so one copied into an op would silently edit an unrelated line of
    this video (the hazard :class:`Seam` describes).  Indented annotation lines
    carry no number and are left alone.
    """
    return _NUMBERED_LINE_RE.sub(rf"[{index}]\1:", transcript)


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min"


def whole_video_block(sections: list[tuple[Segment, str, float | None]]) -> str:
    """The whole finished video's transcript, for the cacheable prefix.

    *sections* are ``(segment, qualified transcript, default runtime seconds)``
    in playback order; a segment's index is its position there.  The closing
    total is left out when any segment's runtime is unknown rather than
    understating the video.
    """
    out = [WHOLE_VIDEO_NOTE]
    for index, (segment, transcript, runtime) in enumerate(sections, start=1):
        header = f"[{index}] {segment_label(segment)}"
        if runtime is not None:
            header += f" — default runtime {_minutes(runtime)}"
        out.append(f"{header}\n{transcript}" if transcript else header)
    runtimes = [runtime for _, _, runtime in sections]
    if runtimes and all(r is not None for r in runtimes):
        out.append(f"Whole video — default runtime {_minutes(sum(runtimes))}")
    return "\n\n".join(out)


@dataclass(frozen=True)
class PriorEdits:
    """The ops already made to one segment that plays earlier.

    *index* is that segment's 1-based position in the finished video, the same
    ``[k]`` the whole-video reference uses, so a line reference here points at
    one line of the finished video and nowhere else.
    """

    index: int
    segment: Segment
    ops: list[DirectorOp]


def _qualified_range(index: int, lines: tuple[int, int]) -> str:
    first, last = lines
    if first == last:
        return f"[{index}]{first}"
    return f"[{index}]{first}-[{index}]{last}"


def _prior_op(index: int, op: DirectorOp) -> str:
    out = f"{op.type} {_qualified_range(index, op.lines)}"
    if op.factor is not None:
        out += f" x{op.factor:g}"
    text = (op.text or "").strip()
    if text:
        out += " 「" + " / ".join(t.strip() for t in text.split("\n") if t.strip()) + "」"
    return out


def format_prior_edits(prior: list[PriorEdits]) -> list[str]:
    """One line per earlier segment: its ops (type, lines, factor, text — no
    notes, which are long), line-ordered."""
    out = []
    for entry in prior:
        ops = sorted(entry.ops, key=lambda op: op.lines)
        body = "; ".join(_prior_op(entry.index, op) for op in ops) or "(no edits)"
        out.append(f"[{entry.index}] {segment_label(entry.segment)}: {body}")
    return out


@dataclass(frozen=True)
class Seam:
    """A neighbouring SEGMENT's lines at the join with this one.

    *lines* is that segment's own text — its last lines on the BEFORE side, its
    first ones on the AFTER side.  Text only, deliberately: a line number here
    would be the NEIGHBOUR's coordinate, and every op the director emits
    addresses its own transcript, so a number it copied out of the seam would
    silently edit an unrelated line of this video.

    *label* names the neighbour the way :func:`order.segment_label` does, so a
    join with another stretch of this very source reads as what it is.
    """

    label: str
    lines: list[str]


@dataclass(frozen=True)
class Neighbour:
    """The segment playing next to this one, and where to read its text.

    Both sides are readable because ``text_filter`` has run for every source
    before ``director`` starts — unlike the prior captions, which can only look
    backwards.
    """

    segment: Segment
    edits: Path


def seam_lines(edit_lines: list[str], count: int, *, last: bool) -> list[str]:
    """The *count* lines at one end of a neighbour's ``_edits.txt``.

    Editing markers and ``{{old->new}}`` patches are stripped (the same view the
    director gets of its own transcript) and blank lines are skipped.  Line
    numbers are deliberately dropped — see :class:`Seam`.
    """
    if count <= 0:
        return []
    lines = [text.strip() for text in clean_for_display(edit_lines) if text.strip()]
    return lines[-count:] if last else lines[:count]


def _seam_block(before: Seam | None, after: Seam | None) -> list[str]:
    """Render the seam context; ``[]`` when neither side exists."""
    if not (before and before.lines) and not (after and after.lines):
        return []
    out = ["", SEAM_NOTE]
    if before and before.lines:
        out.append(
            "Immediately BEFORE this segment in the finished video "
            f"({before.label}, its last lines):"
        )
        out.extend(f"- {text}" for text in before.lines)
    if after and after.lines:
        out.append(f"Immediately AFTER this segment ({after.label}, its first lines):")
        out.extend(f"- {text}" for text in after.lines)
    return out


def _overlaps(part, segment: Segment) -> bool:
    """Does *part* cover any line this segment plays?"""
    if part.stem != segment.stem:
        return False
    if segment.lines is None:
        return True
    return part.lines[0] <= segment.lines[1] and part.lines[1] >= segment.lines[0]


def _clipped(part_lines: tuple[int, int], segment: Segment) -> tuple[int, int]:
    """A part's range as this segment sees it — it plays no more than its own."""
    if segment.lines is None:
        return part_lines
    return (max(part_lines[0], segment.lines[0]), min(part_lines[1], segment.lines[1]))


def _sibling_text(segment: Segment, project_summary: ProjectSummary) -> str:
    """One line about another segment.

    A whole-source segment keeps the video summary it always had; a partial one
    is described by the parts it actually covers, since a video summary would
    describe footage playing somewhere else in the finished video.
    """
    if segment.lines is None:
        own = project_summary.video_summaries.get(segment.stem)
        if own:
            return own
    covered = [p.summary for p in project_summary.parts if _overlaps(p, segment) and p.summary]
    if covered:
        return " / ".join(covered)
    return ""


def _sibling_entry(index: int, segment: Segment, project_summary: ProjectSummary) -> str:
    label = segment_label(segment)
    text = _sibling_text(segment, project_summary)
    return f"- {index}. {label}: {text}" if text else f"- {index}. {label}"


def _directions_by_part(
    own: list, directions: list[PartDirection]
) -> dict[int, list[PartDirection]]:
    """Group this video's directions under the part each one covers.

    ``plan`` may split a summary part into several narrower directions, so a
    direction is matched by line overlap rather than by an exact range; each is
    attached to the part it overlaps most (ties -> the earlier part).
    """
    grouped: dict[int, list[PartDirection]] = {}
    for d in directions:
        best_idx, best_overlap = -1, 0
        for i, p in enumerate(own):
            if p.stem != d.stem:
                continue
            overlap = min(p.lines[1], d.lines[1]) - max(p.lines[0], d.lines[0]) + 1
            if overlap > best_overlap:
                best_idx, best_overlap = i, overlap
        if best_idx >= 0:
            grouped.setdefault(best_idx, []).append(d)
    for entries in grouped.values():
        entries.sort(key=lambda d: d.lines)
    return grouped


def build_director_context(
    project_summary: ProjectSummary,
    directions: list[PartDirection],
    segment: Segment,
    *,
    all_segments: list[Segment] | None = None,
    prior_captions: list[str] | None = None,
    max_prior_captions: int = 0,
    seam_before: Seam | None = None,
    seam_after: Seam | None = None,
    prior_edits: list[PriorEdits] | None = None,
) -> str:
    """Render the context for one SEGMENT: global summary + the parts this
    segment covers (line ranges, summaries, rough directions) + the sibling
    segments.

    ``all_segments`` is the finished video's playback order.  When it is given
    and contains *segment* — and the project has more than one segment — the
    siblings are split into what plays BEFORE and AFTER this one and the header
    states its index, so a whole-project instruction in the editorial brief
    ("explain the rig early on") is readable as being about one particular
    stretch rather than about every video independently.  The unit is the
    segment, not the source: "video 3 of 7" stops meaning anything the moment
    one source plays at two places in the timeline, and another stretch of this
    very source is a neighbour like any other footage.

    ``prior_captions`` are the captions the director already committed on the
    segments playing earlier, so an explanation is not repeated.
    ``max_prior_captions`` keeps only that many of the most recent ones
    (``0`` = no limit).

    ``seam_before``/``seam_after`` are the neighbouring segments' lines at the
    two joins (see :class:`Seam`); either side may be absent (the first segment
    has no predecessor, the last no successor, and a neighbour's ``_edits.txt``
    may be missing on a re-run), and with neither the block is byte-identical to
    before.

    ``prior_edits`` (``director.whole_project_context``) replaces the caption
    list with the ops every earlier segment actually got, so "this was already
    shown fast once" is decidable.  ``None`` keeps the caption block.

    Returns ``""`` when there is nothing to inject (so the director prompt is
    unchanged when the overview is empty).
    """
    stem = segment.stem
    parts = project_summary.parts
    video_summaries = project_summary.video_summaries
    own = [p for p in parts if _overlaps(p, segment)]

    # A single-segment project has no timeline order worth explaining.
    order = list(all_segments or [])
    positioned = len(order) > 1 and segment in order
    index = order.index(segment) + 1 if positioned else 0
    total = len(order)
    captions = list(prior_captions or [])
    if max_prior_captions > 0:
        captions = captions[-max_prior_captions:]
    edits_block = format_prior_edits(prior_edits) if prior_edits is not None else []
    if prior_edits is not None:
        captions = []
    seams = _seam_block(seam_before, seam_after)

    if (
        not project_summary.summary
        and not own
        and not positioned
        and not captions
        and not edits_block
        and not seams
    ):
        return ""

    dirs_by_part = _directions_by_part(own, [d for d in directions if _overlaps(d, segment)])

    out: list[str] = ["Project context (all videos):"]
    if project_summary.summary:
        out.append(f"Overall: {project_summary.summary}")

    if positioned:
        out.append(
            "All segments below are concatenated into ONE finished video in this "
            f"order; you are editing only segment {index} of them."
        )

    if own or positioned:
        header = f'This segment ("{segment_label(segment)}")'
        if positioned:
            header += f" — segment {index} of {total}"
            if index == 1:
                header += ", the FIRST in the finished timeline"
            elif index == total:
                header += ", the LAST in the finished timeline"
        out.append(header + ":")
        if dirs_by_part:
            # Emitted HERE, immediately above the ranges, rather than in
            # DIRECTOR_PROMPT's Rules block: the playback rule that should have
            # prevented this sits at ~55% of the assembled prompt and these
            # ranges arrive at ~82%, and the later, more concrete text won.  A
            # real run copied a plan boundary into 19 of 56 op starts, and into
            # all three timelapses — two of which therefore opened on the line
            # announcing the work and played it back unintelligible.
            out.append(
                "These are section boundaries, not op boundaries: a direction "
                "says what a stretch is FOR, and you choose where each op "
                'actually starts and ends. A direction saying "keep" means '
                "emphasis, not a keep op."
            )
        own_summary = video_summaries.get(stem, "") if segment.lines is None else ""
        if own_summary:
            out.append(f"Summary: {own_summary}")
        for i, p in enumerate(own):
            lines = _clipped(p.lines, segment)
            line = f"- lines {lines[0]}-{lines[1]}: {p.summary}"
            part_dirs = dirs_by_part.get(i, [])
            if len(part_dirs) == 1 and _clipped(part_dirs[0].lines, segment) == lines:
                out.append(line + f" → direction: {part_dirs[0].direction}")
                continue
            if part_dirs:
                # plan split this part: each direction states the lines it covers.
                out.append(line + " → directions:")
                for d in part_dirs:
                    clipped = _clipped(d.lines, segment)
                    out.append(f"    - lines {clipped[0]}-{clipped[1]}: {d.direction}")
            else:
                out.append(line)

    if positioned:
        earlier = [
            _sibling_entry(i + 1, s, project_summary) for i, s in enumerate(order[: index - 1])
        ]
        later = [
            _sibling_entry(index + 1 + i, s, project_summary) for i, s in enumerate(order[index:])
        ]
        out.append("")
        out.append("Earlier in the finished video (already edited):")
        out.extend(earlier or ["- (none)"])
        out.append("Later in the finished video:")
        out.extend(later or ["- (none)"])
    else:
        # One line per other source video (its video summary, else first part's).
        seen: dict[str, str] = {}
        for p in parts:
            if p.stem != stem and p.stem not in seen:
                seen[p.stem] = video_summaries.get(p.stem) or p.summary
        if seen:
            out.append("Other videos:")
            for s, summary in seen.items():
                out.append(f"- {s}: {summary}")

    if captions:
        out.append("")
        out.append("Captions already shown earlier in the finished video:")
        out.extend(f"- {c}" for c in captions)

    if edits_block:
        out.append("")
        out.append("Edits already made to the segments playing earlier:")
        out.extend(edits_block)

    out.extend(seams)

    return "\n".join(out)
