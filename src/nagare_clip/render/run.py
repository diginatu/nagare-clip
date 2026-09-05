"""render stage: the last one, and the only one that never calls a model.

``publish`` decides what the thumbnail *says*; this stage decides nothing at
all.  It reads ``output/publish/publish.json`` -- the hand-editable contract --
composites one image per copy set with ImageMagick, and writes them beside a
contact sheet.

The split exists for one loop: look at a render, change one thing, look again.
The thing being changed sits *between* the two halves (a background that should
have been the next frame over, a headline the model got nearly right), and a
stage boundary is how this pipeline already says "stop here, edit the artifact,
resume".  So::

    $EDITOR output/publish/publish.json
    ./scripts/run_pipeline.sh --from-stage render --to-stage render

costs zero calls, every time.  Nothing in this package imports the LLM
transport, so that is a property of the code rather than of the config.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from nagare_clip.markdown import embed_image
from nagare_clip.render.thumbnail import (
    RenderResult,
    render_sets,
    sets_from_dict,
    shots_from_dict,
)


def empty_render() -> dict[str, Any]:
    """The disabled-stage artifact: the full shape, with nothing in it."""
    return {"renders": [], "skipped": []}


def _load_publish(publish_json: Path) -> Any:
    """``publish.json``, or ``None`` when it is missing/unreadable.

    Never raises: ``render`` runs last, and a project that stopped before
    ``publish`` should get an empty contact sheet rather than a traceback.
    """
    if not publish_json.is_file():
        logging.warning("render: no publish.json at %s; nothing to render", publish_json)
        return None
    try:
        return json.loads(publish_json.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logging.warning("render: could not read %s", publish_json)
        return None


def _no_font_warning(sets: Sequence[Any], fonts: Any) -> str:
    """The note for copy that ImageMagick's default face cannot draw.

    That face has no CJK glyphs and renders such a character as **nothing at
    all** -- not as a box -- so the thumbnail comes back merely looking empty.
    A real run lost every headline this way and it took a person to notice, so
    the file that person is reading is where it has to be said.
    """
    if fonts:
        return ""
    if not any(
        ord(ch) > 0x7F for thumb_set in sets for line in thumb_set.lines for ch in line.text
    ):
        return ""
    return (
        "**No `render.fonts` configured**, and this copy is not plain ASCII. "
        "ImageMagick's default face draws nothing at all for a character it has no "
        "glyph for, so those lines are missing from the images below rather than "
        "showing as boxes. Add a slot to `render.fonts` -- the first one listed is "
        "the face a line falls back to."
    )


def _render_markdown(
    sets: Sequence[Any],
    result: RenderResult,
    enabled: bool,
    markup: str,
    fonts: Any = None,
) -> str:
    """The contact sheet: every set's copy, its background, and the image.

    A set that produced no image keeps its heading and its copy and says
    **why** — this file is what the human reads after editing publish.json, so
    a set that quietly disappears from it turns a one-character typo into a
    mystery whose only trace is a log line.
    """
    if not enabled:
        return "# render\n\nThe render stage is disabled (`render.enabled: false`).\n"
    lines = ["# render", ""]
    warning = _no_font_warning(sets, fonts)
    if warning:
        lines += [warning, ""]
    if not sets:
        lines += ["_(none)_", ""]
    by_index = {r.index: r for r in result.renders}
    reasons = {s.index: s.reason for s in result.skipped}
    for index, thumb_set in enumerate(sets, start=1):
        render = by_index.get(index)
        lines.append(f"## Set {index}")
        lines += [f"- {line.role}: {line.text}" for line in thumb_set.lines]
        if render is not None:
            lines += [
                "",
                f"Background: `{render.background}`",
                "",
                embed_image(render.path, f"Set {index}", 480, markup),
            ]
        else:
            lines += ["", f"**Not rendered:** {reasons.get(index, 'unknown reason')}"]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def run_render(
    publish_json: Path,
    output: Path,
    cfg: dict,
    *,
    run: Callable[[list[str]], str],
    markdown: Path | None = None,
) -> None:
    """Composite every copy set in *publish_json* into *output*'s directory."""
    render_cfg = cfg["render"]
    enabled = bool(render_cfg.get("enabled", True))
    publish_dir = publish_json.parent
    out_dir = output.parent

    sets: list[Any] = []
    result = RenderResult()
    if not enabled:
        logging.info("render: disabled, writing empty render material")
    else:
        data = _load_publish(publish_json)
        sets = sets_from_dict(data)
        result = render_sets(sets, shots_from_dict(data), render_cfg, publish_dir, out_dir, run)

    artifact = {
        "renders": [
            {"set": r.index, "path": r.path, "background": r.background} for r in result.renders
        ],
        "skipped": [{"set": s.index, "reason": s.reason} for s in result.skipped],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logging.info(
        "render: wrote %s (%d image(s), %d skipped)",
        output,
        len(result.renders),
        len(result.skipped),
    )
    if markdown is not None:
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markup = str((cfg.get("general") or {}).get("image_markup", "html"))
        markdown.write_text(
            _render_markdown(sets, result, enabled, markup, render_cfg.get("fonts")),
            encoding="utf-8",
        )
        logging.info("render: wrote %s", markdown)
