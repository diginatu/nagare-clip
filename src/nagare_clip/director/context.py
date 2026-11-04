"""The project's summaries, for the director's view (``director_tools.sh view``).

Its own module because it depends on the ``summary`` stage, while
:mod:`~.transcript` stays free of that import so ``summary`` can import it
without a cycle.

Facts only: the overall summary and each source's whole-video summary.  The
director writes its own plan, so nothing here is an instruction and no line
range is rendered at all.
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
