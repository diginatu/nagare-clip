"""Build the cross-video context block the director injects into its prompt.

Lives in the ``director`` package (not ``director_llm``) because it depends on
the ``summary`` and ``plan`` stages; ``director_llm`` stays free of those imports
so ``summary`` can keep importing it without a cycle.
"""

from __future__ import annotations

from typing import List

from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.summary.summarize import ProjectSummary


def build_director_context(
    project_summary: ProjectSummary,
    directions: List[PartDirection],
    stem: str,
) -> str:
    """Render the context for one video: global summary + this video's parts
    (line ranges, summaries, rough directions) + one-line sibling entries.

    Returns ``""`` when there is nothing to inject (so the director prompt is
    unchanged when the overview is empty).
    """
    parts = project_summary.parts
    own = [p for p in parts if p.stem == stem]
    if not project_summary.summary and not own:
        return ""

    dir_by_key = {(d.stem, d.lines): d.direction for d in directions}

    out: List[str] = ["Project context (all videos):"]
    if project_summary.summary:
        out.append(f"Overall: {project_summary.summary}")

    if own:
        out.append(f'This video ("{stem}"):')
        for p in own:
            line = f"- lines {p.lines[0]}-{p.lines[1]}: {p.summary}"
            direction = dir_by_key.get((p.stem, p.lines), "")
            if direction:
                line += f" → direction: {direction}"
            out.append(line)

    # One line per other source video (first part's summary as a teaser).
    seen: dict[str, str] = {}
    for p in parts:
        if p.stem != stem and p.stem not in seen:
            seen[p.stem] = p.summary
    if seen:
        out.append("Other videos:")
        for s, summary in seen.items():
            out.append(f"- {s}: {summary}")

    return "\n".join(out)
