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
    ThumbRender,
    render_sets,
    sets_from_dict,
    shots_from_dict,
)


def empty_render() -> dict[str, Any]:
    """The disabled-stage artifact: the full shape, with nothing in it."""
    return {"renders": []}


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


def _render_markdown(
    sets: Sequence[Any], renders: Sequence[ThumbRender], enabled: bool, markup: str
) -> str:
    """The contact sheet: every set's copy, its background, and the image."""
    if not enabled:
        return "# render\n\nThe render stage is disabled (`render.enabled: false`).\n"
    lines = ["# render", ""]
    if not renders:
        lines += ["_(none)_", ""]
    by_index = {r.index: r for r in renders}
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
    renders: list[ThumbRender] = []
    if not enabled:
        logging.info("render: disabled, writing empty render material")
    else:
        data = _load_publish(publish_json)
        sets = sets_from_dict(data)
        renders = render_sets(sets, shots_from_dict(data), render_cfg, publish_dir, out_dir, run)

    artifact = {
        "renders": [{"set": r.index, "path": r.path, "background": r.background} for r in renders]
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logging.info("render: wrote %s (%d image(s))", output, len(renders))
    if markdown is not None:
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markup = str(render_cfg.get("image_markup", "html"))
        markdown.write_text(_render_markdown(sets, renders, enabled, markup), encoding="utf-8")
        logging.info("render: wrote %s", markdown)
