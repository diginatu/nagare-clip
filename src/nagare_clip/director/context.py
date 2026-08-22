"""Build the cross-video context block the director injects into its prompt.

Lives in the ``director`` package (not ``director_llm``) because it depends on
the ``summary`` and ``plan`` stages; ``director_llm`` stays free of those imports
so ``summary`` can keep importing it without a cycle.
"""

from __future__ import annotations

from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.summary.summarize import ProjectSummary


def _sibling_text(stem: str, project_summary: ProjectSummary) -> str:
    """One line about another video: its own summary, else its first part's."""
    own = project_summary.video_summaries.get(stem)
    if own:
        return own
    for p in project_summary.parts:
        if p.stem == stem:
            return p.summary
    return ""


def _sibling_entry(index: int, stem: str, project_summary: ProjectSummary) -> str:
    text = _sibling_text(stem, project_summary)
    return f"- {index}. {stem}: {text}" if text else f"- {index}. {stem}"


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
    stem: str,
    *,
    all_stems: list[str] | None = None,
    prior_captions: list[str] | None = None,
    max_prior_captions: int = 0,
) -> str:
    """Render the context for one video: global summary + this video's parts
    (line ranges, summaries, rough directions) + the sibling videos.

    ``all_stems`` is the order the sources are concatenated in (the orchestrator's
    source order, which is what the blender stage lays out).  When it is given and
    contains *stem* — and the project has more than one video — the siblings are
    split into what plays BEFORE and AFTER this one and this video's header states
    its index, so a whole-project instruction in the editorial brief ("explain the
    rig early on") is readable as being about one particular video rather than
    about every video independently.  Without it the flat ``Other videos:`` list is
    rendered exactly as before.

    ``prior_captions`` are the captions the director already committed on the
    videos playing earlier (their ``_director.json`` is written before this call),
    so an explanation is not repeated in every video.  ``max_prior_captions``
    keeps only that many of the most recent ones (``0`` = no limit).

    Returns ``""`` when there is nothing to inject (so the director prompt is
    unchanged when the overview is empty).
    """
    parts = project_summary.parts
    video_summaries = project_summary.video_summaries
    own = [p for p in parts if p.stem == stem]

    # A single-video project has no timeline order worth explaining.
    stems = list(all_stems or [])
    positioned = len(stems) > 1 and stem in stems
    index = stems.index(stem) + 1 if positioned else 0
    total = len(stems)
    captions = list(prior_captions or [])
    if max_prior_captions > 0:
        captions = captions[-max_prior_captions:]

    if not project_summary.summary and not own and not positioned and not captions:
        return ""

    dirs_by_part = _directions_by_part(own, directions)

    out: list[str] = ["Project context (all videos):"]
    if project_summary.summary:
        out.append(f"Overall: {project_summary.summary}")

    if positioned:
        out.append(
            "All videos below are concatenated into ONE finished video in this "
            f"order; you are editing only video {index} of them."
        )

    if own or positioned:
        header = f'This video ("{stem}")'
        if positioned:
            header += f" — video {index} of {total}"
            if index == 1:
                header += ", the FIRST in the finished timeline"
            elif index == total:
                header += ", the LAST in the finished timeline"
        out.append(header + ":")
        own_summary = video_summaries.get(stem, "")
        if own_summary:
            out.append(f"Summary: {own_summary}")
        for i, p in enumerate(own):
            line = f"- lines {p.lines[0]}-{p.lines[1]}: {p.summary}"
            part_dirs = dirs_by_part.get(i, [])
            if len(part_dirs) == 1 and part_dirs[0].lines == p.lines:
                out.append(line + f" → direction: {part_dirs[0].direction}")
                continue
            if part_dirs:
                # plan split this part: each direction states the lines it covers.
                out.append(line + " → directions:")
                for d in part_dirs:
                    out.append(f"    - lines {d.lines[0]}-{d.lines[1]}: {d.direction}")
            else:
                out.append(line)

    if positioned:
        earlier = [
            _sibling_entry(i + 1, s, project_summary) for i, s in enumerate(stems[: index - 1])
        ]
        later = [
            _sibling_entry(index + 1 + i, s, project_summary) for i, s in enumerate(stems[index:])
        ]
        out.append("")
        out.append("Earlier in the finished video (already edited):")
        out.extend(earlier or ["- (none)"])
        out.append("Later in the finished video:")
        out.extend(later or ["- (none)"])
    else:
        # One line per other source video (its video summary, else first part's summary).
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

    return "\n".join(out)
