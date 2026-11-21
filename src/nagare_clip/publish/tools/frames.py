"""The thumbnail shortlist and stills at any display line (no LLM call).

A coding agent calls ``frames`` and then looks at the JPEGs itself.  Every
still of one call is extracted through ONE ``docker compose run``, the way the
stage batches its shortlist: container startup dwarfs the ffmpeg work.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nagare_clip.director.files import read_ops
from nagare_clip.director.tools.project import Project
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.external import (
    build_snapshot_batch_cmd,
    failure_reason,
    run_command,
)
from nagare_clip.pipeline.sources import discover_sources
from nagare_clip.publish.chapters import format_timestamp
from nagare_clip.publish.thumbs import cap_candidates, frame_relpath, select_candidates
from nagare_clip.publish.timeline import Placement, first_surviving_time
from nagare_clip.publish.tools.lines import finished_placements, line_span
from nagare_clip.timing import segment_times

_EPS = 1e-6


@dataclass(frozen=True)
class Still:
    stem: str
    source_time: float
    kind: str  # a shortlist kind, or "line"
    label: str
    line: int | None  # the display line containing the time
    path: str  # relative to the publish stage dir


def _display_line(project: Project, stem: str, t: float) -> int | None:
    """The display line (same stem) whose span contains *t*; earliest wins."""
    for line in project.view.lines:
        if line.stem != stem:
            continue
        span = line_span(project, line.number)
        if span is not None and span.start <= t <= span.end:
            return line.number
    return None


def _seg_times(project: Project, stem: str) -> list:
    path = project.ctx.stage_dir("sentence_split") / f"{stem}.json"
    if not path.is_file():
        return []
    try:
        return segment_times(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        logging.warning("frames: could not read %s", path)
        return []


def _shown(placements: Sequence[Placement], stem: str, t: float) -> bool:
    return first_surviving_time(placements, stem, t, t + _EPS) is not None


def plan_stills(
    project: Project,
    cfg: dict,
    requests: Sequence[tuple[int, float]],
    *,
    notes: list[str] | None = None,
) -> tuple[list[Still], list[str]]:
    """The shortlist (no *requests*), or one still per ``(line, pos)`` request.

    The shortlist leaves out every candidate that is not on the finished
    timeline (before the cap, so the cap fills with shown stills) when that
    timeline is available; what was left out is appended to *notes*.  A
    requested line is never dropped: the agent asked for it.
    """
    problems: list[str] = []
    stills: list[Still] = []
    if not requests:
        stems = list(dict.fromkeys(line.stem for line in project.view.lines))
        ops_by_stem = read_ops(project.ctx, stems)
        candidates = []
        for stem in stems:
            candidates += select_candidates(
                stem, ops_by_stem.get(stem, []), _seg_times(project, stem)
            )
        placements = finished_placements(project)
        if placements is not None:
            shown = [c for c in candidates if _shown(placements, c.stem, c.time)]
            left_out = len(candidates) - len(shown)
            if left_out and notes is not None:
                notes.append(
                    f"{left_out} shortlist still(s) not in the finished video were left out"
                )
            candidates = shown
        for c in cap_candidates(candidates, cfg["publish"]["max_frames"]):
            stills.append(
                Still(
                    c.stem,
                    c.time,
                    c.kind,
                    c.label,
                    _display_line(project, c.stem, c.time),
                    frame_relpath(c.stem, c.time),
                )
            )
        return stills, problems
    for number, pos in requests:
        span = line_span(project, number)
        if span is None:
            problems.append(f"line {number}: no such line (or it has no timing)")
            continue
        if not 0.0 <= pos <= 1.0:
            problems.append(f"line {number}: position {pos} is outside [0, 1]")
            continue
        t = span.start + pos * (span.end - span.start)
        stills.append(
            Still(
                span.stem,
                t,
                "line",
                f"line {number} @{pos:g}",
                _display_line(project, span.stem, t),
                frame_relpath(span.stem, t),
            )
        )
    return stills, problems


def snapshot_jobs(
    stills: Sequence[Still], relative_by_stem: Mapping[str, str]
) -> list[tuple[str, float, str]]:
    return [
        (relative_by_stem[s.stem], s.source_time, "/output/publish/" + s.path)
        for s in stills
        if s.stem in relative_by_stem
    ]


def extract_stills(
    project: Project, stills: list[Still], *, run: Callable = run_command
) -> list[Still]:
    """Write every still in ONE container; drop (and warn about) a missing file."""
    if not stills:
        return []
    ctx = project.ctx
    relative_by_stem = {p.stem: p.name for p in discover_sources(ctx.input_videos_dir)}
    out_dir = ctx.stage_dir("publish")
    for s in stills:
        (out_dir / s.path).parent.mkdir(parents=True, exist_ok=True)
    jobs = snapshot_jobs(stills, relative_by_stem)
    if jobs:
        from nagare_clip.pipeline.cli import PROJECT_ROOT

        batch = build_snapshot_batch_cmd(PROJECT_ROOT, jobs, ctx.cfg["publish"]["frame_width"])
        try:
            run(
                batch.argv,
                env_extra={
                    "INPUT_VIDEOS_DIR": str(ctx.input_videos_dir),
                    "OUTPUT_DIR": str(ctx.output_dir),
                },
                stdin_text=batch.script,
                capture_stderr=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise PipelineError(
                f"frames: extraction in Docker failed ({len(jobs)} job(s)): {failure_reason(exc)}"
            ) from exc
    kept: list[Still] = []
    for s in stills:
        if (out_dir / s.path).is_file():
            kept.append(s)
        else:
            logging.warning(
                "frames: no still written at %.3fs for %s; dropped", s.source_time, s.stem
            )
    return kept


def _timeline_time(placements: Sequence[Placement] | None, s: Still) -> float | None:
    if placements is None:
        return None
    t = first_surviving_time(placements, s.stem, s.source_time, s.source_time + _EPS)
    return None if t is None else round(t, 4)


def write_stills(path: Path, stills: list[Still], placements: Sequence[Placement] | None) -> None:
    rows = [
        {
            "stem": s.stem,
            "source_time": s.source_time,
            "kind": s.kind,
            "label": s.label,
            "line": s.line,
            "path": s.path,
            "timeline_time": _timeline_time(placements, s),
        }
        for s in stills
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_stills(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def format_stills(stills: Sequence[Still], placements: Sequence[Placement] | None) -> str:
    """``line  M:SS|—  kind  label  path`` rows in playback order, untimed last."""
    rows = [(_timeline_time(placements, s), s) for s in stills]
    rows.sort(key=lambda r: (r[0] is None, r[0] if r[0] is not None else 0.0))
    out = []
    for t, s in rows:
        line = "—" if s.line is None else str(s.line)
        when = "—" if t is None else format_timestamp(t)
        out.append(f"{line}  {when}  {s.kind}  {s.label}  {s.path}")
    return "\n".join(out)
