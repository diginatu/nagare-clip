"""The project's summaries, for the director's cached prefix.

Lives in the ``director`` package (not ``director_llm``) because it depends on
the ``summary`` stage; ``director_llm`` stays free of that import so
``summary`` can keep importing it without a cycle.

Facts only: the overall summary and each source's whole-video summary.  The
``plan`` stage's directions used to be rendered here as instructions — the
model with the least information setting the frame for the one with the most —
and their line ranges leaked into op boundaries.  The director now writes its
own plan in its first turn (:data:`~.loop.PLAN_REQUEST`), so nothing here is an
instruction and no line range is rendered at all.
"""

from __future__ import annotations

from nagare_clip.director.display import DisplayView
from nagare_clip.summary.summarize import ProjectSummary

HEADER = "Project context (all videos):"


def project_context_block(project_summary: ProjectSummary, view: DisplayView) -> str:
    """The overall summary, then one line per ``[k]`` source that has a summary.

    ``""`` when there is nothing to say, so an empty ``summary.json`` leaves the
    system message exactly as it was.
    """
    rows: list[str] = []
    if project_summary.summary:
        rows.append(f"Overall: {project_summary.summary}")
    for segment in view.segments:
        text = project_summary.video_summaries.get(segment.stem, "")
        if text:
            rows.append(f"[{segment.index}] {segment.label}: {text}")
    return "\n".join([HEADER, *rows]) if rows else ""
