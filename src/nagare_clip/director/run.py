"""director stage (Pass A): high-level LLM edit operations, one SEGMENT at a time.

A segment is one stretch of one source, as the plan ordered it.  Running per
segment rather than per source makes an op crossing a reorder boundary
unrepresentable, gives improvement 19's position a meaning when one source
plays at two places, and makes improvement 22's seams the *neighbouring
segment* rather than the neighbouring source.

Line numbers stay absolute: ``guided_edit`` and ``intervals`` apply ops to the
whole source file, so a segment starting at line 31 presents its first line as
``31:`` and emits ops in that numbering.

The stage does not write ``{stem}_director.json`` — the orchestrator merges a
source's segments and writes it once.  When ``director.enabled`` is false
(default) every segment returns no ops, which merges to the empty op list the
downstream guided_edit stage treats as a no-op.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.brief import apply_brief
from nagare_clip.director import director_llm as director_llm_mod
from nagare_clip.director.context import (
    Neighbour,
    Seam,
    build_director_context,
    seam_lines,
)
from nagare_clip.director.director_llm import DirectorResult, generate_director_ops
from nagare_clip.gap_context.context import anchor_gaps
from nagare_clip.gap_context.gaps import load_gaps
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.order import Segment, segment_label, segment_unit
from nagare_clip.plan.plan_llm import plan_from_dict
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict
from nagare_clip.timing import segment_silences, segment_times

#: Lines of a neighbouring video shown at each join when ``director.seam_lines``
#: says nothing else.  Small on purpose: the prompt is already long, and a
#: sign-off or a greeting is one or two lines.
DEFAULT_SEAM_LINES = 3


def _seam_line_count(director_cfg: dict) -> int:
    """Read ``director.seam_lines`` defensively (invalid = the default, ``0`` = off)."""
    raw = director_cfg.get("seam_lines", DEFAULT_SEAM_LINES)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return DEFAULT_SEAM_LINES
    return raw


def _seam(neighbour: Neighbour | None, count: int, *, last: bool) -> Seam | None:
    """The neighbouring SEGMENT's lines at one join, read off its ``_edits.txt``.

    The neighbour is a segment, not a source, so the file is sliced to the lines
    that segment actually plays — which is what makes a join with another
    stretch of this very source read correctly.

    Optional throughout: the first segment has no predecessor and the last no
    successor, and a ``--source`` re-run may have neither file on disk — every
    one of those degrades to no seam rather than failing the stage.
    """
    if not neighbour or count <= 0:
        return None
    path = Path(neighbour.edits)
    try:
        all_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logging.warning("director: no readable seam transcript at %s", path)
        return None
    first, last_line = neighbour.segment.lines or (1, len(all_lines))
    lines = seam_lines(all_lines[first - 1 : last_line], count, last=last)
    return Seam(segment_label(neighbour.segment), lines) if lines else None


def _max_prior_captions(director_cfg: dict) -> int:
    """Read ``director.max_prior_captions`` defensively (``0``/invalid = no limit)."""
    raw = director_cfg.get("max_prior_captions", 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def _build_overview_context(
    summary: Path | None,
    plan: Path | None,
    segment: Segment,
    *,
    all_segments: list[Segment] | None = None,
    prior_captions: list[str] | None = None,
    max_prior_captions: int = 0,
    seam_before: Seam | None = None,
    seam_after: Seam | None = None,
) -> str:
    """Load summary/plan artifacts (tolerating missing/empty) and render the
    cross-video context for this segment.  Returns ``""`` if unavailable."""
    project_summary = ProjectSummary(summary="", parts=[])
    if summary and summary.is_file():
        project_summary = summary_from_dict(json.loads(summary.read_text(encoding="utf-8")))
    directions = []
    if plan and plan.is_file():
        directions = plan_from_dict(json.loads(plan.read_text(encoding="utf-8")))
    return build_director_context(
        project_summary,
        directions,
        segment,
        all_segments=all_segments,
        prior_captions=prior_captions,
        max_prior_captions=max_prior_captions,
        seam_before=seam_before,
        seam_after=seam_after,
    )


def _slice(values: list | None, first: int, last: int) -> list | None:
    return None if values is None else values[first - 1 : last]


def run_director(
    edits_txt: Path,
    cfg: dict,
    *,
    segment: Segment,
    all_segments: list[Segment] | None = None,
    summary: Path | None = None,
    plan: Path | None = None,
    json_path: Path | None = None,
    gaps: Path | None = None,
    cuts_txt: Path | None = None,
    prior_captions: list[str] | None = None,
    before: Neighbour | None = None,
    after: Neighbour | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> DirectorResult:
    """One LLM call over one segment.  Returns its ops and whether it succeeded.

    Nothing is written here: a source split across several segments has its ops
    merged by the orchestrator and written once, so a partially edited
    ``{stem}_director.json`` can never reach disk.
    """
    director_cfg = cfg["director"]
    all_lines = edits_txt.read_text(encoding="utf-8").splitlines()
    first, last = segment.lines or (1, len(all_lines))
    edit_lines = all_lines[first - 1 : last]
    unit = segment_unit(segment)

    if not director_cfg.get("enabled", False):
        logging.info("director: disabled, no ops for %s", unit)
        return DirectorResult([], ok=True)

    seam_count = _seam_line_count(director_cfg)
    overview_context = _build_overview_context(
        summary,
        plan,
        segment,
        all_segments=all_segments,
        prior_captions=prior_captions,
        max_prior_captions=_max_prior_captions(director_cfg),
        seam_before=_seam(before, seam_count, last=True),
        seam_after=_seam(after, seam_count, last=False),
    )
    seg_times = None
    if json_path and json_path.is_file():
        try:
            seg_times = segment_times(json.loads(json_path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            logging.warning("director: could not read --json %s", json_path)
    silences = None
    if seg_times and cuts_txt and Path(cuts_txt).is_file():
        silences = segment_silences(seg_times, read_cuts(Path(cuts_txt)))
    # Gaps are anchored against the WHOLE source's times and then restricted to
    # this segment, so one anchoring rule serves a whole source and a slice of
    # one alike.
    gap_list = anchor_gaps(load_gaps(gaps), seg_times or [], segment.lines) if seg_times else []

    logging.info("director: analysing %s (%d line(s)) with LLM", unit, len(edit_lines))
    result = generate_director_ops(
        edit_lines,
        apply_brief(director_cfg, cfg),
        call_llm=director_llm_mod._call_llm,
        overview_context=overview_context,
        recorder=recorder,
        unit=unit,
        seg_times=_slice(seg_times, first, last),
        anchored_gaps=gap_list,
        silences=_slice(silences, first, last),
        first_line=first,
    )
    logging.info("director: %s -> %d operation(s)", unit, len(result.ops))
    return result
