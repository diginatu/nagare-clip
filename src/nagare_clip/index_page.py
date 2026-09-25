"""``index.md`` at the top of the output directory.

The output directory has eighteen top-level entries and eight hundred files
under them, and exactly four of those files are written to be read by a human.
Nothing links to any of them, and the deliverable -- the ``.blend`` -- is named
nowhere at all.  This page is that directory listing with better manners.

It is a convenience page, not a report.  Everything on it is already on disk:
the mtimes, the call counts, the finished-cut arithmetic ``cut_report`` already
owns.  Nothing on it is judgement, so nothing here calls a model -- a property
of the imports rather than of a comment.

**A row states a timestamp, or a dash.  It never explains an absence.** Two
attempts at diagnosing one missing file during the design of this page were
both wrong: the first blamed a partial re-run, the second a silent failure, and
the truth was that the report had simply been committed after that project last
ran.  A timestamp is a fact and is always right; an explanation is a guess by a
writer who cannot see the run, and a wrong one is worse than a dash.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nagare_clip.blender.frames import ordered_sources
from nagare_clip.cut_report.metrics import measure
from nagare_clip.markdown import embed_image
from nagare_clip.order import MANIFEST_NAME, TimelineSegment, read_manifest

INDEX_NAME = "index.md"
ABSENT = "—"
THUMB_WIDTH = 480


@dataclass(frozen=True)
class Row:
    """One listed file: where it is, what it is, and whether it is there."""

    path: str  # relative to the output dir
    label: str  # what the link (or the backticked path) reads as
    description: str
    linkable: bool = True  # a .blend opens in Blender, not in a markdown viewer


def _cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ")


def _mtime(path: Path) -> str:
    """The file's mtime, or a dash. The whole of what a row says about absence."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")
    except OSError:
        return ABSENT


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _publish_description(publish_json: Path) -> str:
    """The title that came out and how many chapters, when publish has run."""
    data = _read_json(publish_json)
    if not isinstance(data, dict):
        return ""
    titles = [t for t in (data.get("titles") or []) if isinstance(t, str) and t.strip()]
    chapters = data.get("chapters") or []
    if not titles:
        return ""
    return f"「{titles[0]}」, {len(chapters)} chapters"


def _report_description(index_md: Path) -> str:
    """``248 calls, 17 dropped-items``, read off the report's own summary line."""
    try:
        text = index_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    calls = re.search(r"^(\d+) call\(s\)", text, re.MULTILINE)
    if calls is None:
        return ""
    dropped = re.search(r"dropped-items: (\d+)", text)
    parts = [f"{calls.group(1)} calls"]
    if dropped is not None:
        parts.append(f"{dropped.group(1)} dropped-items")
    return ", ".join(parts)


def _blend_row(output_dir: Path, blend: Path | None) -> Row:
    """The deliverable. Whatever is on disk; failing that, where it will land."""
    if blend is not None and blend.is_file():
        path = blend
    else:
        built = sorted((output_dir / "blender").glob("*_edited.blend"))
        path = built[0] if built else blend
    if path is None:
        return Row("blender/", "blender/", "the finished cut", linkable=False)
    rel = f"blender/{path.name}"
    return Row(rel, rel, "the finished cut", linkable=False)


def _rows(output_dir: Path, blend: Path | None) -> list[Row]:
    """The files in the order the pipeline writes them.

    ``history.md`` (plan/plan_revise), the ``.blend``, ``publish.md`` and
    ``render.md``.  The LLM report goes last: it covers every call of the run
    rather than one stage.  ``cut_report.md`` has no row: the LLM report
    already embeds it and the headline states its numbers.
    """
    return [
        Row("plan_dialogue/history.md", "history.md", "the plan conversation"),
        _blend_row(output_dir, blend),
        Row(
            "publish/publish.md",
            "publish.md",
            _publish_description(output_dir / "publish" / "publish.json")
            or "titles, chapters, description",
        ),
        Row("render/render.md", "render.md", "the rendered thumbnails"),
        Row(
            "llm_report/index.md",
            "llm_report/index.md",
            _report_description(output_dir / "llm_report" / "index.md") or "the LLM call table",
        ),
    ]


def _name(row: Row, present: bool) -> str:
    """A link when it is there and a viewer can open it; a path otherwise."""
    if present and row.linkable:
        return f"[{row.label}]({row.path})"
    return f"`{row.path}`"


def _headline(output_dir: Path) -> str:
    """``7 sources · 65.5 min → 20.8 min (32%)``, or nothing.

    The numbers are ``cut_report``'s, measured off the same intervals JSONs in
    the same manifest order, so the two files cannot disagree.  A project whose
    ``intervals`` has never run has nothing to say here and says nothing.
    """
    sources = _sources(output_dir)
    if not sources:
        return ""
    m = measure(sources)
    label = "source" if m.sources == 1 else "sources"
    return (
        f"{m.sources} {label} · {m.source_duration / 60.0:.1f} min → "
        f"{m.finished_duration / 60.0:.1f} min ({m.finished_share:.0%})"
    )


def _sources(output_dir: Path) -> list[tuple[str, dict]]:
    """The finished video as ``(stem, sliced intervals data)``, in playback order.

    Read off the whole project's disk rather than off one run's context: this
    page describes the output directory, not the invocation that rewrote it.
    """
    intervals_dir = output_dir / "intervals"
    data_by_stem: dict[str, dict] = {}
    try:
        found = sorted(intervals_dir.glob("*_intervals.json"))
    except OSError:
        return []
    for path in found:
        data = _read_json(path)
        if isinstance(data, dict):
            data_by_stem[path.name[: -len("_intervals.json")]] = data
    entries = read_manifest(intervals_dir / MANIFEST_NAME)
    if not entries:
        entries = [TimelineSegment(stem, 0.0, float("inf")) for stem in sorted(data_by_stem)]
    return ordered_sources(entries, data_by_stem)


def _hooks(publish_json: Path) -> dict[int, str]:
    """Each copy set's hook line, by set number, from ``publish.json``.

    The hook is already written and already on disk; no call is made for it.
    """
    data = _read_json(publish_json)
    if not isinstance(data, dict):
        return {}
    hooks: dict[int, str] = {}
    for index, thumb_set in enumerate(data.get("thumbnail_copy") or [], start=1):
        if not isinstance(thumb_set, dict):
            continue
        for line in thumb_set.get("lines") or []:
            text = line.get("text") if isinstance(line, dict) else None
            if line.get("role") == "hook" and isinstance(text, str) and text.strip():
                hooks[index] = text.strip()
                break
    return hooks


def _thumbnail_alt(number: int, hooks: dict[int, str]) -> str:
    """``Set 1 — <hook>``: what the picture says, not just which one it is.

    It goes in the alt rather than in a caption line under the image because
    the copy is burned into the thumbnail: a person looking at the page reads
    it off the picture, and a visible line repeating it is redundant to them.
    Alt is invisible to that reader and is the only description that reaches a
    screen reader or a model reading this file, so it is where the hook earns
    its place.  A set whose copy has no hook keeps the bare label.
    """
    hook = hooks.get(number, "")
    return f"Set {number} — {hook}" if hook else f"Set {number}"


def _thumbnails(output_dir: Path, markup: str) -> list[str]:
    """The rendered sets, inline -- the one thing on the page worth looking at."""
    data = _read_json(output_dir / "render" / "render.json")
    renders = data.get("renders") or [] if isinstance(data, dict) else []
    hooks = _hooks(output_dir / "publish" / "publish.json")
    embeds = [
        embed_image(
            f"render/{r['path']}", _thumbnail_alt(r.get("set", i), hooks), THUMB_WIDTH, markup
        )
        for i, r in enumerate(renders, start=1)
        if isinstance(r, dict) and isinstance(r.get("path"), str)
    ]
    if not embeds:
        return []
    return ["", "## thumbnails", "", *embeds]


def build_index(
    output_dir: Path | str, cfg: dict[str, Any] | None = None, *, blend: Path | None = None
) -> str:
    """The page, as markdown. Reads only what is on disk; never raises on absence."""
    output_dir = Path(output_dir)
    markup = str(((cfg or {}).get("general") or {}).get("image_markup", "html"))
    project = output_dir.resolve().parent.name or output_dir.resolve().name

    lines = [f"# {project}", ""]
    headline = _headline(output_dir)
    if headline:
        lines += [headline, ""]
    lines += ["| | |", "|---|---|"]
    for row in _rows(output_dir, blend):
        path = output_dir / row.path
        present = path.is_file()
        lines.append(
            f"| {_cell(_name(row, present))} — {_cell(row.description)} | {_mtime(path)} |"
        )
    lines += _thumbnails(output_dir, markup)
    return "\n".join(lines).rstrip("\n") + "\n"


def write_index(
    output_dir: Path | str, cfg: dict[str, Any] | None = None, *, blend: Path | None = None
) -> Path:
    """Write ``index.md`` at the top of *output_dir* and return its path."""
    output_dir = Path(output_dir)
    path = output_dir / INDEX_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_index(output_dir, cfg, blend=blend), encoding="utf-8")
    return path
