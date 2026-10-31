"""Load a project's director view off disk, exactly as the director stage does."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.director.display import DisplayView, build_display_view
from nagare_clip.director.loop import Range, order_ranges
from nagare_clip.director.run import SegmentTranscript, load_segment_transcript
from nagare_clip.order import Segment, identity_segments, normalise, order_from_dict
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia, project_stems
from nagare_clip.pipeline.stages import (
    _read_director_ops,
    _read_director_plan,
    director_inputs,
)

EDITS_SUFFIX = "_edits.txt"


@dataclass(frozen=True)
class Project:
    """One project's director view, plus where its director files live.

    The view is always shooting order — one ``[k]`` block per source, numbered
    once — whatever order is in force, as it is for the LLM director.
    """

    ctx: PipelineContext
    view: DisplayView
    transcripts: list[SegmentTranscript]

    @property
    def stems(self) -> list[str]:
        return [seg.stem for seg in self.view.segments]

    @property
    def director_dir(self) -> Path:
        return self.ctx.stage_dir("director")

    @property
    def line_counts(self) -> dict[str, int]:
        return {stem: len(t.edit_lines) for stem, t in zip(self.stems, self.transcripts)}

    def segment_index(self, stem: str) -> int | None:
        return next((s.index for s in self.view.segments if s.stem == stem), None)


def _context(cfg: dict) -> PipelineContext:
    p = cfg["pipeline"]
    output_dir = Path(p["output_dir"]).resolve()
    input_dir = Path(p["input_videos_dir"]).resolve()
    edits_dir = output_dir / "text_filter"
    stems = project_stems(input_dir) or sorted(
        f.name.removesuffix(EDITS_SUFFIX) for f in edits_dir.glob(f"*{EDITS_SUFFIX}")
    )
    return PipelineContext(
        cfg=cfg,
        project_root=Path.cwd(),
        config_path=None,
        input_videos_dir=input_dir,
        output_dir=output_dir,
        sources=[SourceMedia(Path(s), s, s) for s in stems],
        from_index=0,
        to_index=0,
    )


def load_project(cfg: dict) -> Project:
    """The project *cfg* names, loaded through the director's own loaders.

    Raises :class:`PipelineError` when the pipeline has not reached
    ``text_filter`` for every source yet.
    """
    ctx = _context(cfg)
    if not ctx.stems:
        raise PipelineError(
            f"no sources found in {ctx.input_videos_dir} or {ctx.stage_dir('text_filter')}: "
            "run the pipeline through text_filter first "
            "(./scripts/run_pipeline.sh --to-stage text_filter)"
        )
    segments = identity_segments(ctx.stems)
    inputs = director_inputs(ctx, segments)
    missing = [str(i.edits) for i in inputs if not i.edits.is_file()]
    if missing:
        raise PipelineError(
            "missing text_filter output: "
            + ", ".join(missing)
            + " — run ./scripts/run_pipeline.sh --to-stage text_filter first"
        )
    transcripts = [load_segment_transcript(i) for i in inputs]
    view = build_display_view([(i.segment, t) for i, t in zip(inputs, transcripts)])
    return Project(ctx=ctx, view=view, transcripts=transcripts)


@dataclass(frozen=True)
class CurrentState:
    """What ``director/`` holds now, in display numbering.

    *ops* are per segment index in SOURCE coordinates (what ``edit_state``
    takes); *order* is display ranges, ``[]`` for shooting order; *unmapped*
    lists ops or an order that no longer map onto the view (a transcript or
    ``director.silence_line_min`` changed underneath them).
    """

    ops: dict[int, list[DirectorOp]]
    order: list[Range]
    plan: str
    unmapped: list[str]


def read_order_file(project: Project) -> list[Segment] | None:
    path = project.director_dir / "order.json"
    if not path.is_file():
        return None
    try:
        return order_from_dict(json.loads(path.read_text(encoding="utf-8"))) or None
    except (OSError, ValueError) as e:
        logging.warning("could not read %s: %s", path, e)
        return None


def current_state(project: Project) -> CurrentState:
    """The director files on disk, mapped onto *project*'s view."""
    view = project.view
    unmapped: list[str] = []
    ops: dict[int, list[DirectorOp]] = {}
    for stem, stem_ops in _read_director_ops(project.ctx, project.stems).items():
        index = project.segment_index(stem)
        for op in stem_ops:
            a = view.from_source(index, op.lines[0], silence=op.gap_start) if index else None
            b = view.from_source(index, op.lines[1], silence=op.gap_end) if index else None
            if a is None or b is None:
                unmapped.append(f"{stem}_director.json: {op.type} {list(op.lines)}")
                continue
            ops.setdefault(index, []).append(op)
    order: list[Range] = []
    segments = read_order_file(project)
    if segments is not None:
        segments = normalise(segments, project.line_counts)
        if segments != identity_segments(project.stems):
            order = order_ranges(view, segments)
            if not order:
                unmapped.append("order.json does not tile the view; shooting order shown")
    return CurrentState(
        ops=ops, order=order, plan=_read_director_plan(project.ctx), unmapped=unmapped
    )


def display_range(project: Project, index: int, op: DirectorOp) -> Range | None:
    view = project.view
    a = view.from_source(index, op.lines[0], silence=op.gap_start)
    b = view.from_source(index, op.lines[1], silence=op.gap_end)
    return None if a is None or b is None else (a, b)
