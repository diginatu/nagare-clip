"""Build the cross-video context block the director injects into its prompt.

Lives in the ``director`` package (not ``director_llm``) because it depends on
the ``summary`` and ``plan`` stages; ``director_llm`` stays free of those imports
so ``summary`` can keep importing it without a cycle.
"""

from __future__ import annotations

from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.summary.summarize import ProjectSummary


def build_director_context(
    project_summary: ProjectSummary,
    directions: list[PartDirection],
    stem: str,
) -> str:
    """Render the context for one video: global summary + this video's parts
    (line ranges, summaries, rough directions) + one-line sibling entries.

    Returns ``""`` when there is nothing to inject (so the director prompt is
    unchanged when the overview is empty).
    """
    parts = project_summary.parts
    video_summaries = project_summary.video_summaries
    own = [p for p in parts if p.stem == stem]
    if not project_summary.summary and not own:
        return ""

    dir_by_key = {(d.stem, d.lines): d.direction for d in directions}

    out: list[str] = ["Project context (all videos):"]
    if project_summary.summary:
        out.append(f"Overall: {project_summary.summary}")

    if own:
        out.append(f'This video ("{stem}"):')
        own_summary = video_summaries.get(stem, "")
        if own_summary:
            out.append(f"Summary: {own_summary}")
        for p in own:
            line = f"- lines {p.lines[0]}-{p.lines[1]}: {p.summary}"
            direction = dir_by_key.get((p.stem, p.lines), "")
            if direction:
                line += f" → direction: {direction}"
            out.append(line)

    # One line per other source video (its video summary, else first part's summary).
    seen: dict[str, str] = {}
    for p in parts:
        if p.stem != stem and p.stem not in seen:
            seen[p.stem] = video_summaries.get(p.stem) or p.summary
    if seen:
        out.append("Other videos:")
        for s, summary in seen.items():
            out.append(f"- {s}: {summary}")

    return "\n".join(out)
