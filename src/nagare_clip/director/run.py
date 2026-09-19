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
from dataclasses import dataclass
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.brief import apply_brief
from nagare_clip.director import director_llm as director_llm_mod
from nagare_clip.director.context import (
    Neighbour,
    PriorEdits,
    Seam,
    build_director_context,
    qualify_line_numbers,
    seam_lines,
    whole_video_block,
)
from nagare_clip.director.director_llm import (
    DirectorResult,
    clean_for_display,
    generate_director_ops,
    render_transcript,
    speech_seconds,
)
from nagare_clip.gap_context.context import anchor_gaps
from nagare_clip.gap_context.gaps import Gap, load_gaps
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
    prior_edits: list[PriorEdits] | None = None,
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
        prior_edits=prior_edits,
    )


def _slice(values: list | None, first: int, last: int) -> list | None:
    return None if values is None else values[first - 1 : last]


@dataclass(frozen=True)
class SegmentInputs:
    """Where one segment's transcript and its timing annotations live."""

    segment: Segment
    edits: Path
    json_path: Path | None = None
    gaps: Path | None = None
    cuts_txt: Path | None = None


@dataclass(frozen=True)
class SegmentTranscript:
    """One segment's lines and annotations, sliced to the lines it plays.

    ``seg_times``/``silences`` cover the segment only; ``gaps`` are anchored
    against the WHOLE source's times and then restricted to the segment, so one
    anchoring rule serves a whole source and a slice of one alike.
    """

    edit_lines: list[str]
    first_line: int
    seg_times: list | None
    silences: list | None
    gaps: list[tuple[int, Gap]]

    def render(self) -> str:
        """The numbered transcript exactly as this segment's own call shows it."""
        return render_transcript(
            clean_for_display(self.edit_lines),
            self.seg_times,
            self.silences,
            self.gaps,
            self.first_line,
        )

    def default_runtime(self) -> float | None:
        """Seconds the segment plays with no op at all; ``None`` if untimed."""
        if self.seg_times is None or len(self.seg_times) != len(self.edit_lines):
            return None
        speech = speech_seconds(self.seg_times, self.silences)
        if any(sec is None for sec in speech):
            return None
        return sum(speech)


def load_segment_transcript(inputs: SegmentInputs) -> SegmentTranscript:
    """Read one segment's transcript and annotations off disk.

    Every annotation is optional: a missing ``--json`` means no brackets, a
    missing cuts file no speech/silence split, a missing gaps file no gap lines.
    """
    segment = inputs.segment
    all_lines = inputs.edits.read_text(encoding="utf-8").splitlines()
    first, last = segment.lines or (1, len(all_lines))
    seg_times = None
    if inputs.json_path and inputs.json_path.is_file():
        try:
            seg_times = segment_times(json.loads(inputs.json_path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            logging.warning("director: could not read --json %s", inputs.json_path)
    silences = None
    if seg_times and inputs.cuts_txt and Path(inputs.cuts_txt).is_file():
        silences = segment_silences(seg_times, read_cuts(Path(inputs.cuts_txt)))
    gap_list = (
        anchor_gaps(load_gaps(inputs.gaps), seg_times or [], segment.lines) if seg_times else []
    )
    return SegmentTranscript(
        edit_lines=all_lines[first - 1 : last],
        first_line=first,
        seg_times=_slice(seg_times, first, last),
        silences=_slice(silences, first, last),
        gaps=gap_list,
    )


def whole_video_reference(segments: list[SegmentInputs]) -> str:
    """The whole finished video's transcript, rendered ONCE per run.

    Every segment is rendered by :meth:`SegmentTranscript.render` — the same
    renderer as its own call's editable transcript — with each numbered line
    qualified by its segment index (see :func:`qualify_line_numbers`).  The
    result goes inside the cacheable prefix, so it must be passed unchanged to
    every segment's call.  A segment whose transcript cannot be read keeps its
    header, so the indices stay the finished video's positions.
    """
    sections = []
    for index, inputs in enumerate(segments, start=1):
        try:
            transcript = load_segment_transcript(inputs)
        except OSError:
            logging.warning("director: no readable transcript at %s", inputs.edits)
            sections.append((inputs.segment, "", None))
            continue
        sections.append(
            (
                inputs.segment,
                qualify_line_numbers(transcript.render(), index),
                transcript.default_runtime(),
            )
        )
    return whole_video_block(sections)


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
    whole_video: str = "",
    prior_edits: list[PriorEdits] | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> DirectorResult:
    """One LLM call over one segment.  Returns its ops and whether it succeeded.

    Nothing is written here: a source split across several segments has its ops
    merged by the orchestrator and written once, so a partially edited
    ``{stem}_director.json`` can never reach disk.

    ``whole_video`` (from :func:`whole_video_reference`) and ``prior_edits`` are
    ``director.whole_project_context``'s additions; the orchestrator passes them
    only when that flag is on, and absent they leave the request unchanged.
    """
    director_cfg = cfg["director"]
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
        prior_edits=prior_edits,
    )
    transcript = load_segment_transcript(
        SegmentInputs(segment, edits_txt, json_path=json_path, gaps=gaps, cuts_txt=cuts_txt)
    )
    user_header = ""
    if whole_video:
        index = (all_segments or [segment]).index(segment) + 1
        user_header = f"Edit segment [{index}] — its lines below are the ones your ops address:"

    logging.info("director: analysing %s (%d line(s)) with LLM", unit, len(transcript.edit_lines))
    result = generate_director_ops(
        transcript.edit_lines,
        apply_brief(director_cfg, cfg),
        call_llm=director_llm_mod._call_llm,
        overview_context=overview_context,
        recorder=recorder,
        unit=unit,
        seg_times=transcript.seg_times,
        anchored_gaps=transcript.gaps,
        silences=transcript.silences,
        first_line=transcript.first_line,
        reference=whole_video,
        user_header=user_header,
    )
    logging.info("director: %s -> %d operation(s)", unit, len(result.ops))
    return result
