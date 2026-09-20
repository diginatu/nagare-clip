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
import math
from dataclasses import dataclass, field
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
    DirectorOp,
    DirectorResult,
    _max_keep_lines,
    clean_for_display,
    generate_director_ops,
    keep_limit_note,
    render_transcript,
    speech_seconds,
)
from nagare_clip.director.display import DisplayView, build_display_view
from nagare_clip.director.loop import LoopState, ReplyResult, apply_reply, next_request
from nagare_clip.director.preview import preview_turn
from nagare_clip.director.silence_lines import (
    DEFAULT_SILENCE_LINE_MIN,
    SilenceLine,
    build_silence_lines,
)
from nagare_clip.gap_context.context import anchor_gaps
from nagare_clip.gap_context.gaps import Gap, load_gaps
from nagare_clip.llm_client import CACHEABLE_PREFIX_KEY, with_trace_meta
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    UNPARSEABLE,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.order import Segment, segment_label, segment_unit
from nagare_clip.plan.plan_llm import plan_from_dict
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict
from nagare_clip.timing import segment_silences, segment_times

logger = logging.getLogger(__name__)

#: Default for ``director.chunk_lines``: how many display lines one turn is
#: asked to review.  Kept beside the loop that uses it rather than in the
#: config schema alone, so an unconfigured call behaves like the stage.
DEFAULT_CHUNK_LINES = 40

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


def silence_line_min(director_cfg: dict) -> float:
    """Read ``director.silence_line_min`` defensively (invalid = the default).

    ``0`` is honoured as "every between-line silence gets a line"; a negative
    or non-numeric value is a broken config, not an instruction.
    """
    raw = director_cfg.get("silence_line_min", DEFAULT_SILENCE_LINE_MIN)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        return DEFAULT_SILENCE_LINE_MIN
    return float(raw)


@dataclass(frozen=True)
class SegmentInputs:
    """Where one segment's transcript and its timing annotations live."""

    segment: Segment
    edits: Path
    json_path: Path | None = None
    gaps: Path | None = None
    cuts_txt: Path | None = None
    #: ``director.silence_line_min``: the shortest wait between two lines the
    #: transcript shows as a silence line of its own.
    silence_line_min: float = DEFAULT_SILENCE_LINE_MIN


@dataclass(frozen=True)
class SegmentTranscript:
    """One segment's lines and annotations, sliced to the lines it plays.

    ``seg_times``/``silences`` cover the segment only; ``gaps`` are anchored
    against the WHOLE source's times and then restricted to the segment, so one
    anchoring rule serves a whole source and a slice of one alike.

    ``silence_lines`` are the waits between this segment's lines long enough to
    be shown as lines of their own; the ``gaps`` left here are the descriptions
    none of them claimed (a silence INSIDE a line), still annotated above the
    line whose bracket reports it.
    """

    edit_lines: list[str]
    first_line: int
    seg_times: list | None
    silences: list | None
    gaps: list[tuple[int, Gap]]
    silence_lines: list[SilenceLine] = field(default_factory=list)

    def render(self) -> str:
        """The numbered transcript exactly as this segment's own call shows it."""
        return render_transcript(
            clean_for_display(self.edit_lines),
            self.seg_times,
            self.silences,
            self.gaps,
            self.first_line,
            silence_lines=self.silence_lines,
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
    data: dict = {}
    seg_times = None
    if inputs.json_path and inputs.json_path.is_file():
        try:
            data = json.loads(inputs.json_path.read_text(encoding="utf-8"))
            seg_times = segment_times(data)
        except (ValueError, OSError):
            logging.warning("director: could not read --json %s", inputs.json_path)
    silences = None
    if seg_times and inputs.cuts_txt and Path(inputs.cuts_txt).is_file():
        silences = segment_silences(seg_times, read_cuts(Path(inputs.cuts_txt)))
    gap_list = (
        anchor_gaps(load_gaps(inputs.gaps), seg_times or [], segment.lines) if seg_times else []
    )
    silence_lines: list[SilenceLine] = []
    if seg_times:
        # The word times, not the segment bounds: this is the silence the
        # intervals stage drops and "n~" edits.
        silence_lines, gap_list = build_silence_lines(
            data,
            gap_list,
            min_seconds=inputs.silence_line_min,
            lines=(first, last),
        )
    return SegmentTranscript(
        edit_lines=all_lines[first - 1 : last],
        first_line=first,
        seg_times=_slice(seg_times, first, last),
        silences=_slice(silences, first, last),
        gaps=gap_list,
        silence_lines=silence_lines,
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
        SegmentInputs(
            segment,
            edits_txt,
            json_path=json_path,
            gaps=gaps,
            cuts_txt=cuts_txt,
            silence_line_min=silence_line_min(director_cfg),
        )
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
        silence_lines=transcript.silence_lines,
        reference=whole_video,
        user_header=user_header,
    )
    logging.info("director: %s -> %d operation(s)", unit, len(result.ops))
    return result


#: Heads the whole-video transcript inside the cached system prefix.  One line:
#: what follows is data, and the protocol is stated in ``DIRECTOR_PROMPT``.
VIEW_HEADER = (
    "The whole finished video below, every segment in playback order under one "
    "numbering. [k] heads each segment; your ops address these numbers."
)


@dataclass(frozen=True)
class ConversationResult:
    """What one director conversation produced.

    *ops* are keyed by SOURCE stem in source coordinates — what
    ``{stem}_director.json`` holds — and are complete for every source of the
    video, empty list included, whatever *ok* says: the caller writes them
    BEFORE it fails, so reaching the cap leaves a usable edit behind.
    """

    ops: dict[str, list[DirectorOp]]
    ok: bool = True
    error: str = ""
    reviewed_through: int = 0
    turns: int = 0


def system_message(director_cfg: dict, view: DisplayView) -> dict[str, str]:
    """The one system message, identical on every turn of the conversation.

    Prompt, the keep-limit note and the whole video, in that order — and the
    WHOLE of it is declared cacheable: nothing in it varies per turn, so the
    breakpoint sits at its end and every turn after the first reads the cache
    instead of paying for the transcript again.
    """
    prompt = director_cfg.get("prompt", "")
    max_keep_lines = _max_keep_lines(director_cfg)
    if max_keep_lines > 0:
        prompt = f"{prompt}\n\n{keep_limit_note(max_keep_lines)}"
    content = f"{prompt}\n\n{VIEW_HEADER}\n\n{view.render()}"
    return {"role": "system", "content": content, CACHEABLE_PREFIX_KEY: content}


def turn_cap(lines: int, chunk_lines: int) -> int:
    """Two turns per chunk: one to review it, one to come back and fix it."""
    return math.ceil(lines / max(chunk_lines, 1)) * 2


def chunk_lines(director_cfg: dict) -> int:
    """Read ``director.chunk_lines`` defensively (invalid = the default)."""
    raw = director_cfg.get("chunk_lines", DEFAULT_CHUNK_LINES)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return DEFAULT_CHUNK_LINES
    return raw


def _answer(
    view: DisplayView,
    transcripts: list[SegmentTranscript],
    state: LoopState,
    result: ReplyResult,
) -> str:
    """What the code sends back after a reply: the refusals, then the playback."""
    parts = [result.refusal] if result.refusal else []
    parts.append(
        preview_turn(view, transcripts, state.ops, segments=result.segments, drops=result.drops)
    )
    return "\n\n".join(parts)


def run_director_conversation(
    inputs: list[SegmentInputs],
    cfg: dict,
    *,
    call_llm: director_llm_mod.CallLLM | None = None,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "director",
) -> ConversationResult:
    """Edit the whole video in ONE conversation, and return its ops per source.

    *inputs* are every segment of the finished video, in playback order.  The
    transcripts are loaded once, numbered once (:func:`build_display_view`),
    and rendered once into the system message; each turn then asks for an
    approximate range (:func:`~.loop.next_request`), reads the reply into the
    accumulated state (:func:`~.loop.apply_reply`) and answers with what those
    ops will play (:func:`~.preview.preview_turn`).

    Nothing is written here.  Ending badly — the turn cap, or a turn that fails
    every retry — comes back as ``ok=False`` with the ops accepted so far, for
    the caller to write BEFORE it fails the run: the user then continues by
    hand from ``guided_edit`` instead of losing the conversation.
    """
    director_cfg = cfg["director"]
    # Resolved at call time, never bound as a default: the stage and the tests
    # both reach the real client by patching the module attribute.
    call = call_llm or director_llm_mod._call_llm
    ops_by_stem: dict[str, list[DirectorOp]] = {i.segment.stem: [] for i in inputs}
    if not director_cfg.get("enabled", False):
        logging.info("director: disabled, no ops for %d segment(s)", len(inputs))
        return ConversationResult(ops_by_stem)

    transcripts = [load_segment_transcript(i) for i in inputs]
    view = build_display_view([(i.segment, t) for i, t in zip(inputs, transcripts)])
    chunk = chunk_lines(director_cfg)
    cap = turn_cap(len(view.lines), chunk)
    max_keep_lines = _max_keep_lines(director_cfg)
    logging.info(
        "director: %d display line(s) over %d segment(s), %d line(s) per turn, cap %d turn(s)",
        len(view.lines),
        len(inputs),
        chunk,
        cap,
    )

    stage_cfg = apply_brief(director_cfg, cfg)
    messages: list[dict[str, str]] = [system_message(stage_cfg, view)]
    state = LoopState()
    answer = ""
    error = ""

    recorder.begin(unit)
    traced = with_trace_meta(stage_cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(traced)
    for turn in range(cap):
        request = next_request(view, state, chunk)
        base = f"{answer}\n\n{request}" if answer else request
        section = f"turn {turn + 1}"
        result: ReplyResult | None = None
        complaint = ""
        for attempt in range(attempts):
            attempt_cfg = cfg_for_attempt(traced, attempt)
            # The complaint is the previous attempt's own parse error, handed
            # back: re-sending the identical message at a higher temperature is
            # the one thing that cannot use what went wrong.
            content = f"{base}\n\n{complaint}" if complaint else base
            ask = {"role": "user", "content": content}
            turn_messages = messages + [ask]
            try:
                reply = call(turn_messages, attempt_cfg)
            except Exception as e:  # noqa: BLE001 - recoverable
                logger.warning("director: turn %d call failed", turn + 1, exc_info=True)
                recorder.attempt(
                    unit=unit,
                    attempt=attempt,
                    total=attempts,
                    section=section,
                    messages=[ask],
                    error=str(e),
                    outcome=LLM_ERROR,
                    reason="LLM call failed",
                    cfg=attempt_cfg,
                )
                complaint = ""
                continue
            parsed = apply_reply(view, state, reply, max_keep_lines=max_keep_lines)
            outcome, reason = OK, ""
            if parsed.error:
                outcome, reason = UNPARSEABLE, parsed.error
            elif parsed.drops:
                outcome, reason = DROPPED_ITEMS, "; ".join(parsed.drops)
            elif not parsed.ops:
                outcome, reason = OK_EMPTY, ""
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                section=section,
                messages=[ask],
                response=reply,
                outcome=outcome,
                reason=reason,
                cfg=attempt_cfg,
            )
            if parsed.error:
                complaint = f"That reply could not be used: {parsed.error}"
                continue
            messages = turn_messages + [{"role": "assistant", "content": reply}]
            result = parsed
            break
        if result is None:
            error = f"turn {turn + 1} failed after all {attempts} attempt(s)"
            break
        if result.done:
            logging.info("director: done after %d turn(s)", turn + 1)
            break
        answer = _answer(view, transcripts, state, result)
        recorder.attempt(
            unit=unit,
            attempt=0,
            total=1,
            section=f"{section} preview",
            messages=[],
            response=answer,
            outcome=OK,
            deterministic=True,
            usage={},
        )
        logging.info(
            "director: turn %d/%d reviewed through display line %d of %d (%d op(s))",
            turn + 1,
            cap,
            state.reviewed_through,
            len(view.lines),
            sum(len(v) for v in state.ops.values()),
        )
    else:
        error = f"the turn cap ({cap} turns) was reached"

    for index, ops in state.ops.items():
        ops_by_stem[inputs[index - 1].segment.stem].extend(ops)
    outcome = LLM_ERROR if error else OK
    recorder.flush_unit(unit, outcome=outcome, reason=error)
    if error:
        logger.error("director: %s", error)
    return ConversationResult(
        ops=ops_by_stem,
        ok=not error,
        error=error,
        reviewed_through=state.reviewed_through,
        turns=state.turns,
    )
