"""The director's files: where they live, how they are read and written.

``director/`` holds the director's decisions — one ``{stem}_director.json``
per source (ops in source coordinates), ``order.json`` (the playback order)
and ``plan.md`` (the plan in prose).  The agent skill's ``write`` tool writes
them, ``guided_edit`` checks them on resume, and ``guided_edit``,
``intervals`` (through the order) and ``publish`` read them.  All three are
hand-editable.

:func:`director_inputs` names where the director's view is read from, so the
tools and the checkpoint see the same transcript.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.director.load import SegmentInputs, silence_line_min
from nagare_clip.director.ops import DirectorOp, ops_from_dict, ops_to_dict
from nagare_clip.order import Segment, order_from_dict, segments_to_dict
from nagare_clip.pipeline.runner import PipelineContext

ORDER_FILE = "order.json"
PLAN_FILE = "plan.md"
PLAN_HEADING = "# The director's plan"
OPS_SUFFIX = "_director.json"


def director_dir(ctx: PipelineContext) -> Path:
    return ctx.stage_dir("director")


def ops_path(ctx: PipelineContext, stem: str) -> Path:
    return director_dir(ctx) / f"{stem}{OPS_SUFFIX}"


def order_path(ctx: PipelineContext) -> Path:
    return director_dir(ctx) / ORDER_FILE


def plan_path(ctx: PipelineContext) -> Path:
    return director_dir(ctx) / PLAN_FILE


def write_ops(ctx: PipelineContext, stem: str, ops: list[DirectorOp]) -> None:
    """One source's ops, line-sorted, written once."""
    path = ops_path(ctx, stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(ops_to_dict(sorted(ops, key=lambda op: op.lines)), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    logging.debug("director: wrote %s (%d operation(s))", path, len(ops))


def read_ops(ctx: PipelineContext, stems: list[str]) -> dict[str, list[DirectorOp]]:
    """Every readable ``{stem}_director.json`` of *stems*; a missing or
    unreadable one is left out (logged)."""
    out: dict[str, list[DirectorOp]] = {}
    for stem in stems:
        path = ops_path(ctx, stem)
        if not path.is_file():
            continue
        try:
            out[stem] = ops_from_dict(json.loads(path.read_text(encoding="utf-8")), None)
        except (OSError, ValueError) as e:
            logging.warning("director: could not read %s (%s); no ops for %s", path, e, stem)
    return out


def write_plan(ctx: PipelineContext, plan: str) -> None:
    """The plan, for the human to read — and to edit."""
    if not plan:
        return
    path = plan_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{PLAN_HEADING}\n\n{plan}\n", encoding="utf-8")


def read_plan(ctx: PipelineContext) -> str:
    """``plan.md`` without its heading; ``""`` when there is none."""
    try:
        text = plan_path(ctx).read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.strip().splitlines()
    if lines and lines[0].strip() == PLAN_HEADING:
        lines = lines[1:]
    return "\n".join(lines).strip()


def write_order(ctx: PipelineContext, order: list[Segment]) -> None:
    """The playback order — hand-editable."""
    path = order_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"order": segments_to_dict(order)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_order(ctx: PipelineContext) -> list[Segment] | None:
    """``order.json``'s order, or ``None`` when there is no usable one."""
    path = order_path(ctx)
    if not path.is_file():
        return None
    try:
        return order_from_dict(json.loads(path.read_text(encoding="utf-8"))) or None
    except (OSError, ValueError) as e:
        logging.warning("director: could not read %s (%s)", path, e)
        return None


def missing_files(ctx: PipelineContext, stems: list[str]) -> list[Path]:
    """The director files a resumed run needs and does not have.

    The director's output "exists" when ``order.json`` and a
    ``{stem}_director.json`` for every one of *stems* (the whole project) are
    there — what ``write`` always writes.  ``plan.md`` is not required: it is
    for the human (and ``publish``, which treats it as optional).  The
    checkpoint uses it to word its stop message.
    """
    wanted = [order_path(ctx)] + [ops_path(ctx, stem) for stem in stems]
    return [path for path in wanted if not path.is_file()]


def director_inputs(ctx: PipelineContext, segments: list[Segment]) -> list[SegmentInputs]:
    """Where each of *segments*' transcript and annotations live.

    The one place the director's inputs are named: the checkpoint and the
    agent skill's tools (:mod:`nagare_clip.director.tools`) both read them
    through here, so the two see the same view.
    """
    text_filter_dir = ctx.stage_dir("text_filter")
    return [
        SegmentInputs(
            segment,
            text_filter_dir / f"{segment.stem}_edits.txt",
            json_path=ctx.stage_dir("sentence_split") / f"{segment.stem}.json",
            gaps=ctx.stage_dir("gap_context") / f"{segment.stem}_gaps.json",
            cuts_txt=ctx.stage_dir("audio_silence") / f"{segment.stem}_cuts.txt",
            silence_line_min=silence_line_min(ctx.cfg["director"]),
            intervals_cfg=ctx.cfg["intervals"],
        )
        for segment in segments
    ]
