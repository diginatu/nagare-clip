"""The publish.json shape and the reviewable publish.md (pure, no LLM)."""

from __future__ import annotations

from typing import Any

from nagare_clip.markdown import embed_image
from nagare_clip.publish.chapters import format_timestamp


def empty_publish() -> dict[str, Any]:
    """The full ``publish.json`` shape, with nothing in it."""
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


def render_publish_markdown(data: dict[str, Any], markup: str = "html") -> str:
    """The reviewable file: copy-pasteable description, everything else beside it."""
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
        for i, thumb_set in enumerate(data["thumbnail_copy"], start=1):
            lines.append(f"### Set {i}")
            lines += [
                f"- {line['role']}: {line['text']}" if line.get("role") else f"- {line['text']}"
                for line in thumb_set["lines"]
            ]
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
            still = embed_image(thumb["path"], thumb["label"], 240, markup)
            lines.append(
                f"| {at} | {thumb['source_time']:.1f}s | {thumb['kind']} | "
                f"{thumb['label']} | {still} |"
            )
    else:
        lines.append("_(none)_")
    return "\n".join(lines).rstrip("\n") + "\n"
