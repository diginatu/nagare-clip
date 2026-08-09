"""publish stage: what a human needs to actually upload the video.

Runs once project-wide after ``blender``.  Everything it emits already exists
in the pipeline — the summaries know what the video is about, the brief knows
who it is for, the intervals know what survived — but nothing past the
``.blend`` used any of it, so it was retyped by hand.

Two halves:

- **copy** (one LLM call): several title candidates, a description lead, the
  chapter titles, and alternative thumbnail-copy sets.
- **timing** (deterministic): chapter timestamps taken from the finished
  timeline, which is the one thing that can only be computed here.

Compositing the chosen frame and copy into the actual thumbnail stays a
project-level script: fonts, colours, shadows and layout are taste and change
from video to video.  What this stage owes that script is the copy and the
frame shortlist.

When ``publish.enabled`` is false (default) an empty artifact is written and no
LLM or Docker call is made.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from nagare_clip.brief import apply_brief
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.plan.plan_llm import PartDirection, plan_from_dict
from nagare_clip.publish.chapters import (
    MIN_CHAPTER_SECONDS,
    Chapter,
    build_chapters,
    chapter_issues,
    chapter_timestamps,
    format_timestamp,
    render_chapter_lines,
)
from nagare_clip.publish.publish_llm import (
    PublishCopy,
    generate_publish_copy,
    thumbnail_copy_to_dict,
)
from nagare_clip.publish.thumbs import ThumbShot
from nagare_clip.publish.timeline import (
    Placement,
    build_placements,
    first_surviving_time,
    total_duration,
)
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict


def empty_publish() -> dict[str, Any]:
    """The disabled-stage artifact: the full shape, with nothing in it."""
    return {
        "titles": [],
        "lead": "",
        "description": "",
        "chapters": [],
        "chapters_qualify": False,
        "chapter_issues": [],
        "thumbnail_copy": [],
        "thumbnails": [],
    }


def _load_json(path: Path | None) -> Any:
    """Parsed JSON, or ``None`` when the file is missing/unreadable.

    Every input of this stage is optional: it runs last, so an earlier stage
    may have been skipped or its output deleted, and the title candidates and
    thumbnail copy do not depend on the timeline.
    """
    if path is None or not Path(path).is_file():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logging.warning("publish: could not read %s", path)
        return None


def _load_placements(stems: Sequence[str], intervals_paths: Sequence[Path]) -> list[Placement]:
    sources: list[tuple[str, dict]] = []
    for stem, path in zip(stems, intervals_paths):
        data = _load_json(path)
        if isinstance(data, dict):
            sources.append((stem, data))
        else:
            logging.warning("publish: no intervals for %s; its parts get no chapter", stem)
    return build_placements(sources)


def _chapter_entries(
    project: ProjectSummary, placements: Sequence[Placement], copy: PublishCopy
) -> list[tuple[float, str]]:
    """``(finished-timeline seconds, title)`` per part that survived the cut.

    Titles are the LLM's, keyed by the part's **original** 1-based index (the
    numbering it was shown), falling back to the part's own summary — so a
    dropped part never shifts the others' titles.
    """
    entries: list[tuple[float, str]] = []
    for i, part in enumerate(project.parts):
        time = first_surviving_time(placements, part.stem, part.start, part.end)
        if time is None:
            logging.info(
                "publish: part %d (%s lines %d-%d) is not in the finished cut; no chapter",
                i + 1,
                part.stem,
                part.lines[0],
                part.lines[1],
            )
            continue
        entries.append((time, copy.chapter_titles.get(i + 1, part.summary)))
    return entries


def _thumbnail_entries(
    thumbs: Sequence[ThumbShot], placements: Sequence[Placement]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for shot in thumbs:
        timeline = first_surviving_time(placements, shot.stem, shot.time, shot.time + 0.001)
        out.append(
            {
                "stem": shot.stem,
                "source_time": round(shot.time, 3),
                "timeline_time": round(timeline, 3) if timeline is not None else None,
                "kind": shot.kind,
                "label": shot.label,
                "path": shot.path,
            }
        )
    return out


def _render_markdown(data: dict[str, Any], enabled: bool) -> str:
    """The reviewable file: copy-pasteable description, everything else beside it."""
    if not enabled:
        return "# publish\n\nThe publish stage is disabled (`publish.enabled: false`).\n"
    lines = ["# publish", ""]

    lines += ["## Title candidates", ""]
    lines += [f"{i + 1}. {title}" for i, title in enumerate(data["titles"])] or ["_(none)_"]
    lines += ["", "## Description", "", "```"]
    lines += data["description"].split("\n")
    lines += ["```", ""]

    lines += ["## Chapters", ""]
    if data["chapters_qualify"]:
        lines.append("YouTube will render these as chapters.")
    else:
        lines.append(
            "YouTube will **not** draw a segmented progress bar for these "
            "(it still auto-links the timestamps, so a viewer can jump):"
        )
        lines += [f"- {issue}" for issue in data["chapter_issues"]]
    lines.append("")

    lines += ["## Thumbnail copy", ""]
    if data["thumbnail_copy"]:
        for i, thumb_set in enumerate(data["thumbnail_copy"]):
            lines.append(f"### Set {i + 1}")
            lines += [f"- {line['role']}: {line['text']}" for line in thumb_set]
            lines.append("")
    else:
        lines += ["_(none)_", ""]

    lines += ["## Thumbnail frame candidates", ""]
    if data["thumbnails"]:
        lines += ["| finished | source | kind | label | frame |", "| --- | --- | --- | --- | --- |"]
        for thumb in data["thumbnails"]:
            at = (
                format_timestamp(thumb["timeline_time"])
                if thumb["timeline_time"] is not None
                else "—"
            )
            lines.append(
                f"| {at} | {thumb['source_time']:.1f}s | {thumb['kind']} | "
                f"{thumb['label']} | `{thumb['path']}` |"
            )
    else:
        lines.append("_(none)_")
    return "\n".join(lines).rstrip("\n") + "\n"


def run_publish(
    summary_json: Path,
    output: Path,
    cfg: dict,
    *,
    stems: Sequence[str],
    intervals_paths: Sequence[Path],
    plan_json: Path | None = None,
    overlay_texts: dict[str, list[str]] | None = None,
    thumbs: Sequence[ThumbShot] | None = None,
    markdown: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    publish_cfg = cfg["publish"]
    enabled = bool(publish_cfg.get("enabled", False))

    if not enabled:
        logging.info("publish: disabled, writing empty publish material")
        data = empty_publish()
    else:
        project = summary_from_dict(_load_json(summary_json))
        directions: list[PartDirection] = plan_from_dict(_load_json(plan_json))
        flat_overlays = [text for stem in stems for text in (overlay_texts or {}).get(stem, [])]
        copy = generate_publish_copy(
            project,
            apply_brief(publish_cfg, cfg),
            directions=directions,
            overlay_texts=flat_overlays,
            recorder=recorder,
        )

        min_chapter = float(publish_cfg.get("min_chapter_duration", MIN_CHAPTER_SECONDS))
        placements = _load_placements(stems, intervals_paths)
        total = total_duration(placements)
        chapters: list[Chapter] = build_chapters(
            _chapter_entries(project, placements, copy), total, min_duration=min_chapter
        )
        stamps = chapter_timestamps(chapters)
        chapter_lines = render_chapter_lines(chapters)
        issues = chapter_issues(chapters, total, min_duration=min_chapter)
        description = "\n\n".join(part for part in (copy.lead, "\n".join(chapter_lines)) if part)

        data = {
            "titles": copy.titles,
            "lead": copy.lead,
            "description": description,
            "chapters": [
                {"time": round(chapter.time, 3), "timestamp": stamp, "title": chapter.title}
                for chapter, stamp in zip(chapters, stamps)
            ],
            "chapters_qualify": not issues,
            "chapter_issues": issues,
            "thumbnail_copy": thumbnail_copy_to_dict(copy),
            "thumbnails": _thumbnail_entries(thumbs or [], placements),
        }
        logging.info(
            "publish: %d title(s), %d chapter(s), %d thumbnail copy set(s), %d frame(s)",
            len(data["titles"]),
            len(data["chapters"]),
            len(data["thumbnail_copy"]),
            len(data["thumbnails"]),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logging.info("publish: wrote %s", output)
    if markdown is not None:
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(_render_markdown(data, enabled), encoding="utf-8")
        logging.info("publish: wrote %s", markdown)
