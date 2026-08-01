"""publish outputs: the machine-readable ``publish.json`` and the review file.

``publish.json`` is the contract a project-level script reads (the thumbnail
compositor takes its copy from ``thumbnail_copy`` and its frame from
``thumbnail_frames``, instead of hardcoding both as ``make_thumb.sh`` does
today). ``publish.md`` is the same content laid out for a human to read and
copy from — the description is rendered as one paste-ready block, because
YouTube's chapter parsing only works on the description exactly as written.
"""

from __future__ import annotations

from typing import Any

from nagare_clip.publish.chapters import Chapter, format_chapters, format_timestamp
from nagare_clip.publish.publish_llm import PublishCopy
from nagare_clip.publish.thumbs import ThumbCandidate


def empty_publish_data() -> dict[str, Any]:
    """The disabled-stage artifact: every key present, nothing in it."""
    return {
        "titles": [],
        "lead": "",
        "description": "",
        "chapters": [],
        "chapter_issues": [],
        "thumbnail_copy": [],
        "thumbnail_frames": [],
        "duration_sec": 0.0,
    }


def build_description(lead: str, chapters: list[Chapter]) -> str:
    """Lead paragraph + the timestamp list, as it should be pasted."""
    blocks = [lead.strip()] if lead.strip() else []
    if chapters:
        blocks.append("\n".join(format_chapters(chapters)))
    return "\n\n".join(blocks)


def publish_to_dict(
    copy: PublishCopy,
    chapters: list[Chapter],
    issues: list[str],
    frames: list[ThumbCandidate],
    duration: float,
) -> dict[str, Any]:
    return {
        "titles": list(copy.titles),
        "lead": copy.lead,
        "description": build_description(copy.lead, chapters),
        "chapters": [
            {
                "time": format_timestamp(c.start),
                "seconds": round(c.start, 3),
                "title": c.title,
                "stem": c.stem,
                "part_index": c.part_index,
            }
            for c in chapters
        ],
        "chapter_issues": list(issues),
        "thumbnail_copy": [
            {"lines": [{"role": ln.role, "text": ln.text} for ln in s.lines]}
            for s in copy.thumbnail_copy
        ],
        "thumbnail_frames": [
            {
                "path": c.relpath,
                "stem": c.stem,
                "source_time": round(c.source_time, 3),
                "timeline_time": round(c.timeline_time, 3),
                "reason": c.reason,
            }
            for c in frames
        ],
        "duration_sec": round(duration, 3),
    }


def render_markdown(data: dict[str, Any], *, enabled: bool = True) -> str:
    """The reviewable file. Upload stays a human action; this is the material."""
    out: list[str] = ["# Publish material", ""]
    if not enabled:
        out += ["_publish stage disabled — nothing generated._", ""]
        return "\n".join(out)

    out += ["## Title candidates", ""]
    if data["titles"]:
        out += [f"{i}. {t}" for i, t in enumerate(data["titles"], start=1)]
    else:
        out.append("_none generated_")
    out.append("")

    out += ["## Description (paste as-is)", "", "```", data["description"] or "", "```", ""]
    if data["chapter_issues"]:
        out += [
            "> YouTube will not render these timestamps as chapters:",
            *[f"> - {issue}" for issue in data["chapter_issues"]],
            ">",
            "> They still auto-link in the description, so a viewer can jump; only "
            "the segmented progress bar is missing.",
            "",
        ]

    out += ["## Thumbnail copy", ""]
    if data["thumbnail_copy"]:
        for i, s in enumerate(data["thumbnail_copy"], start=1):
            out.append(f"{i}. " + " / ".join(f"{ln['role']}: {ln['text']}" for ln in s["lines"]))
    else:
        out.append("_none generated_")
    out.append("")

    out += ["## Thumbnail frame candidates", ""]
    if data["thumbnail_frames"]:
        out += ["| At | Source | Frame | Why |", "|---|---|---|---|"]
        out += [
            "| {t} | {stem} @ {src:.1f}s | `{path}` | {reason} |".format(
                t=format_timestamp(f["timeline_time"]),
                stem=f["stem"],
                src=f["source_time"],
                path=f["path"],
                reason=str(f["reason"]).replace("|", r"\|"),
            )
            for f in data["thumbnail_frames"]
        ]
    else:
        out.append("_none extracted_")
    out.append("")
    return "\n".join(out)
