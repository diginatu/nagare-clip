"""publish stage: what a human needs to actually upload the video.

Runs once project-wide after ``blender``.  Everything it emits already exists
in the pipeline — the summaries know what the video is about, the brief knows
who it is for, the intervals know what survived — but nothing past the
``.blend`` used any of it, so it was retyped by hand.

Three phases, in this order and deliberately not folded together:

- **look** (one vision call per candidate still, cached by content hash):
  what is actually legible in each frame, written to ``frames.json``.
  Depends on nothing but the frame.
- **copy** (one LLM call): several title candidates, a description lead, the
  chapter titles, and alternative thumbnail-copy sets as plain role+text. It
  is shown no frames: the headlines come from the story, not from the pictures
  on hand.
- **pair** (one text-only LLM call): per set, which frame it goes on and where
  and in what colour the text sits — from the descriptions, never from images,
  and naming its frame by index. Separate from the copy call because two dozen
  frame descriptions in front of that one makes it caption the photographs
  instead.
- **timing** (deterministic): chapter timestamps taken from the finished
  timeline, which is the one thing that can only be computed here.

Compositing is deliberately NOT done here.  The LLM writes each copy set's
look in ImageMagick's own vocabulary (fill/stroke/pointsize/gravity/offset/
shadow) into ``publish.json``, and the ``render`` stage -- which never calls a
model -- turns that into images.  So a hook or a background can be hand-edited
and re-rendered without paying for the copy again.  What still stays manual is
picking which rendered set to ship and uploading it.

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
from nagare_clip.markdown import embed_image
from nagare_clip.publish.chapters import (
    MIN_CHAPTER_SECONDS,
    Chapter,
    build_chapters,
    chapter_issues,
    chapter_timestamps,
    format_timestamp,
    render_chapter_lines,
)
from nagare_clip.publish.describe_frames import (
    FrameDescription,
    describe_frames,
    frames_to_dict,
    load_frames,
)
from nagare_clip.publish.pairing import apply_pairing, generate_pairing
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


def _ordered_stems(ordered: Sequence[tuple[str, dict]]) -> list[str]:
    """Each source once, in the order it first plays in the finished video."""
    seen: list[str] = []
    for stem, _ in ordered:
        if stem not in seen:
            seen.append(stem)
    return seen


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
    # Parts are listed in transcript order; the finished video may play them in
    # another.  build_chapters drops an entry that does not advance, so an
    # unsorted list would silently delete a chapter after a reorder.
    return sorted(entries, key=lambda entry: entry[0])


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


def _render_markdown(data: dict[str, Any], enabled: bool, markup: str = "html") -> str:
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
        for i, thumb_set in enumerate(data["thumbnail_copy"], start=1):
            lines.append(f"### Set {i}")
            lines += [f"- {line['role']}: {line['text']}" for line in thumb_set["lines"]]
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


def _pairing_cfg(publish_cfg: dict, pairing_cfg: dict, cfg: dict) -> dict[str, Any]:
    """The pairing call's config: publish's, overridden by what pairing states.

    It runs on publish's own provider and model, so it must also be sampled
    the way that model requires -- an unset key means "inherit", not "use my
    own idea of a good temperature".  A hardcoded default beside an inherited
    model is how a real run got `temperature=0.4` rejected on every attempt by
    a model that accepts only `1`.

    Font slots come from `render:`, because a font is part of the look and the
    renderer is what resolves a slot name.
    """
    stated = {k: v for k, v in pairing_cfg.items() if v is not None}
    return {**publish_cfg, **stated, "fonts": (cfg.get("render") or {}).get("fonts")}


def run_publish(
    summary_json: Path,
    output: Path,
    cfg: dict,
    *,
    ordered: Sequence[tuple[str, dict]],
    plan: str = "",
    overlay_texts: dict[str, list[str]] | None = None,
    thumbs: Sequence[ThumbShot] | None = None,
    frames_json: Path | None = None,
    markdown: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    publish_cfg = cfg["publish"]
    enabled = bool(publish_cfg.get("enabled", False))

    if not enabled:
        logging.info("publish: disabled, writing empty publish material")
        data = empty_publish()
    else:
        # Look before writing: describing a still depends on nothing but the
        # still, and every frame whose bytes already have a description costs
        # no call -- so re-running publish for better copy is cheap.
        frames: list[FrameDescription] = []
        if frames_json is not None:
            frames = describe_frames(
                thumbs or [],
                frames_json.parent,
                publish_cfg.get("describe_frames") or {},
                previous=load_frames(frames_json),
                recorder=recorder,
            )
            frames_json.parent.mkdir(parents=True, exist_ok=True)
            frames_json.write_text(
                json.dumps(frames_to_dict(frames), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            logging.info("publish: wrote %s", frames_json)

        project = summary_from_dict(_load_json(summary_json))
        flat_overlays = [
            text for stem in _ordered_stems(ordered) for text in (overlay_texts or {}).get(stem, [])
        ]
        copy = generate_publish_copy(
            project,
            apply_brief(publish_cfg, cfg),
            plan=plan,
            overlay_texts=flat_overlays,
            recorder=recorder,
        )

        # Pair last: the copy is written blind, then matched to a picture.
        # A set the pairing does not decide keeps the built-in preset look,
        # which is exactly how a project with no pairing at all renders.
        pairing_cfg = publish_cfg.get("pairing") or {}
        if pairing_cfg.get("enabled", True):
            pairing = generate_pairing(
                copy,
                frames,
                _pairing_cfg(publish_cfg, pairing_cfg, cfg),
                recorder=recorder,
            )
            copy.thumbnail_copy = apply_pairing(copy.thumbnail_copy, pairing, frames)

        min_chapter = float(publish_cfg.get("min_chapter_duration", MIN_CHAPTER_SECONDS))
        placements = build_placements(ordered)
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
        markup = str((cfg.get("general") or {}).get("image_markup", "html"))
        markdown.write_text(_render_markdown(data, enabled, markup), encoding="utf-8")
        logging.info("publish: wrote %s", markdown)
