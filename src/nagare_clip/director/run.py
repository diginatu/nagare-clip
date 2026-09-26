"""director stage (Pass A): high-level LLM edit operations for the whole video.

ONE conversation reads the finished video under one display numbering
(:mod:`nagare_clip.director.display`), asks for an approximate range per turn
(:mod:`nagare_clip.director.loop`) and answers each reply with what those ops
will play (:mod:`nagare_clip.director.preview`).  The per-segment path it
replaced -- nine independent calls, each with the seam lines of its
neighbours and the ops already made before it -- is gone.

Line numbers on DISK stay absolute and per source: ``guided_edit`` and
``intervals`` apply ops to the whole source file, so every op is converted back
out of display coordinates before it is written.

Nothing is written here; the orchestrator writes one file per source.  When
``director.enabled`` is false (default) every source returns no ops, which the
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
from nagare_clip.config import DEFAULTS
from nagare_clip.director import director_llm as director_llm_mod
from nagare_clip.director.context import project_context_block
from nagare_clip.director.director_llm import (
    DirectorOp,
    _max_keep_lines,
    keep_limit_note,
    speech_seconds,
)
from nagare_clip.director.display import DisplayView, build_display_view
from nagare_clip.director.loop import (
    LoopState,
    ReplyResult,
    apply_reply,
    next_request,
    order_ranges,
    order_segments,
    request_summary,
)
from nagare_clip.director.preview import edit_state
from nagare_clip.director.silence_lines import (
    DEFAULT_SILENCE_LINE_MIN,
    SilenceLine,
    build_silence_lines,
)
from nagare_clip.edit_lines import parse_edit_lines
from nagare_clip.edit_lines import silence_line_min as silence_line_min  # re-export
from nagare_clip.gap_context.context import anchor_gaps
from nagare_clip.gap_context.gaps import Gap, load_gaps
from nagare_clip.intervals.keep import dropped_ranges
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
from nagare_clip.order import Segment, identity_segments, normalise
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict
from nagare_clip.timing import segment_silences, segment_times

logger = logging.getLogger(__name__)

#: Default for ``director.chunk_lines``: how many display lines one turn is
#: asked to review.  Kept beside the loop that uses it rather than in the
#: config schema alone, so an unconfigured call behaves like the stage.
DEFAULT_CHUNK_LINES = 40


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
    #: ``director.silence_line_min``: the shortest wait between two lines the
    #: transcript shows as a silence line of its own.
    silence_line_min: float = DEFAULT_SILENCE_LINE_MIN
    #: The ``intervals:`` config section: a line's ``Ys silence`` is what that
    #: stage drops, so it is priced with that stage's settings.  ``None`` is
    #: the section's defaults.
    intervals_cfg: dict | None = None


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

    def default_runtime(self) -> float | None:
        """Seconds the segment plays with no op at all; ``None`` if untimed."""
        if self.seg_times is None or len(self.seg_times) != len(self.edit_lines):
            return None
        speech = speech_seconds(self.seg_times, self.silences)
        if any(sec is None for sec in speech):
            return None
        return sum(speech)


_DROPS_CACHE: dict[str, list[tuple[float, float]]] = {}


def source_drops(
    data: dict,
    edit_lines: list[str],
    cuts_txt: Path | None,
    intervals_cfg: dict | None,
) -> list[tuple[float, float]]:
    """What ``intervals`` drops from this source with no director op.

    :func:`~nagare_clip.intervals.keep.dropped_ranges` over the same inputs the
    stage reads — the edit lines, the cut list, the ``intervals:`` settings —
    so a line's bracket prices exactly the footage the render drops.  Cached
    on those inputs: a source split into several segments is computed once.
    """
    cuts = read_cuts(Path(cuts_txt)) if cuts_txt and Path(cuts_txt).is_file() else []
    ivl = intervals_cfg if intervals_cfg is not None else DEFAULTS["intervals"]
    key = json.dumps([data, edit_lines, cuts, ivl], sort_keys=True, ensure_ascii=False)
    if key not in _DROPS_CACHE:
        try:
            drops = dropped_ranges(data, ivl, edit_lines=edit_lines, cut_ranges=cuts)
        except ValueError as exc:
            # intervals would refuse these edits outright; price the source
            # without them rather than not at all.
            logging.warning("director: edit lines do not sync (%s); pricing without them", exc)
            drops = dropped_ranges(data, ivl, cut_ranges=cuts)
        _DROPS_CACHE[key] = drops
    return _DROPS_CACHE[key]


def load_segment_transcript(inputs: SegmentInputs) -> SegmentTranscript:
    """Read one segment's transcript and annotations off disk.

    Every annotation is optional: a missing ``--json`` means no brackets, a
    missing cuts file no speech/silence split, a missing gaps file no gap lines.
    """
    segment = inputs.segment
    # Speech lines only: a silence line is not a transcript line, and counting
    # one would shift every line after it off its segment.
    all_lines = parse_edit_lines(
        inputs.edits.read_text(encoding="utf-8").splitlines()
    ).speech_lines()
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
    if seg_times:
        silences = segment_silences(
            seg_times, source_drops(data, all_lines, inputs.cuts_txt, inputs.intervals_cfg)
        )
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


#: Heads the whole-video transcript inside the cached system prefix.  One line:
#: what follows is data, and the protocol is stated in ``DIRECTOR_PROMPT``.
#: Shooting order, whatever order is in force: the order is conversation
#: state, shown in the edit each turn, never the shape of this transcript.
VIEW_HEADER = (
    "The whole video below, every source in shooting order under one "
    "numbering. [k] heads each source; your ops and your order address these numbers."
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
    #: The playback order the conversation ended with, in source coordinates
    #: and normalised — the seed when the model never sent one.  Empty only
    #: when there was nothing to order.
    order: list[Segment] = field(default_factory=list)
    #: The plan in force when the conversation ended; ``""`` if none was written.
    plan: str = ""
    ok: bool = True
    error: str = ""
    reviewed_through: int = 0
    turns: int = 0


def project_context(summary: Path | None, view: DisplayView) -> str:
    """The summary stage's facts about the project, for the whole video.

    The ``plan`` stage's directions are deliberately NOT read: the director
    writes its own plan in its first turn.  A missing or empty ``summary.json``
    gives ``""``, which leaves the system message exactly as it was.
    """
    project_summary = ProjectSummary(summary="", parts=[])
    if summary and summary.is_file():
        project_summary = summary_from_dict(json.loads(summary.read_text(encoding="utf-8")))
    return project_context_block(project_summary, view)


def system_message(director_cfg: dict, view: DisplayView, context: str = "") -> dict[str, str]:
    """The one system message, identical on every turn of the conversation.

    Prompt, the keep-limit note, the project context and the whole video, in
    that order — and the WHOLE of it is declared cacheable: nothing in it varies
    per turn, so the breakpoint sits at its end and every turn after the first
    reads the cache instead of paying for the transcript again.

    *context* is :func:`project_context`'s block.  It is instruction, so it
    goes ABOVE the transcript, which is data; and it is the same on every turn,
    which is why it belongs in here rather than in a user message.
    """
    prompt = director_cfg.get("prompt", "")
    max_keep_lines = _max_keep_lines(director_cfg)
    if max_keep_lines > 0:
        prompt = f"{prompt}\n\n{keep_limit_note(max_keep_lines)}"
    content = "\n\n".join(p for p in (prompt, context, VIEW_HEADER, view.render()) if p)
    return {"role": "system", "content": content, CACHEABLE_PREFIX_KEY: content}


def _line_counts(
    inputs: list[SegmentInputs], transcripts: list[SegmentTranscript]
) -> dict[str, int]:
    """Lines per source, from the whole-source segments the view is built of."""
    return {
        i.segment.stem: len(t.edit_lines)
        for i, t in zip(inputs, transcripts)
        if i.segment.lines is None
    }


def _seed_ranges(view: DisplayView, seed: list[Segment], counts: dict[str, int]):
    """The seed order as display ranges; ``[]`` (shooting order) if it is one."""
    seed = normalise(seed, counts)
    if seed == identity_segments([s.stem for s in view.segments]):
        return []
    ranges = order_ranges(view, seed)
    if not ranges:
        logging.warning("director: the seed order does not map onto the view; shooting order")
    return ranges


def turn_cap(lines: int, chunk_lines: int) -> int:
    """The planning turn, then two per chunk: one to review it, one to fix it."""
    return math.ceil(lines / max(chunk_lines, 1)) * 2 + 1


def chunk_lines(director_cfg: dict) -> int:
    """Read ``director.chunk_lines`` defensively (invalid = the default)."""
    raw = director_cfg.get("chunk_lines", DEFAULT_CHUNK_LINES)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return DEFAULT_CHUNK_LINES
    return raw


def _ask(
    view: DisplayView,
    transcripts: list[SegmentTranscript],
    state: LoopState,
    previous: ReplyResult | None,
    request: str,
) -> str:
    """The live user message: what the edit IS, then what to do next.

    The conversation carries no preview of its own past any more — the earlier
    asks are trimmed to one line each (:func:`~.loop.request_summary`) — so
    this block is the whole picture, recomputed from the accumulated ops every
    turn.  It leads, because it is what the request is about.

    *previous* is the reply this message answers: its refusals belong at the
    top (they are about what was just sent, not about the edit), and its
    parser drops go in the state's footer where they always did.
    """
    parts = [previous.refusal] if previous is not None and previous.refusal else []
    drops = previous.drops if previous is not None else ()
    parts.append(
        edit_state(view, transcripts, state.ops, drops=drops, order=state.order, plan=state.plan)
    )
    parts.append(request)
    return "\n\n".join(parts)


def run_director_conversation(
    inputs: list[SegmentInputs],
    cfg: dict,
    *,
    summary: Path | None = None,
    order: list[Segment] | None = None,
    call_llm: director_llm_mod.CallLLM | None = None,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "director",
) -> ConversationResult:
    """Edit the whole video in ONE conversation, and return its ops per source.

    *summary* is the summary stage's ``summary.json``; it becomes the project
    context block inside the cached prefix.  The first turn asks for the
    model's own plan (:data:`~.loop.PLAN_REQUEST`); :attr:`ConversationResult.plan`
    is the one in force at the end.

    *inputs* are the segments of the view — one whole source each, in shooting
    order, as the stage builds them: the view never changes shape, so a display
    number names one line for the whole conversation.  *order* seeds the
    playback order (the plan's, in source coordinates); the model may replace
    it on any turn, and :attr:`ConversationResult.order` is what it ended
    with.  The transcripts are loaded once, numbered once (:func:`build_display_view`),
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
    seed = list(order) if order else identity_segments([i.segment.stem for i in inputs])
    if not director_cfg.get("enabled", False):
        logging.info("director: disabled, no ops for %d segment(s)", len(inputs))
        return ConversationResult(ops_by_stem, order=seed)

    transcripts = [load_segment_transcript(i) for i in inputs]
    view = build_display_view([(i.segment, t) for i, t in zip(inputs, transcripts)])
    counts = _line_counts(inputs, transcripts)
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
    context = project_context(summary, view)
    system = system_message(stage_cfg, view, context)
    # The model's own replies, each under the one line of the ask it answered.
    # The replies are its trajectory — which turn made which op, what it has
    # already rewritten — and no recomputed state can show that.  The asks are
    # trimmed because what they carried IS recomputed: a stale state block read
    # as current is the exact failure this design removes.
    history: list[dict[str, str]] = []
    state = LoopState(order=_seed_ranges(view, seed, counts))
    previous: ReplyResult | None = None
    error = ""

    recorder.begin(unit)
    traced = with_trace_meta(stage_cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(traced)
    for turn in range(cap):
        request = next_request(view, state, chunk)
        summary_line = request_summary(view, state, chunk)
        base = _ask(view, transcripts, state, previous, request)
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
            turn_messages = [system] + history + [ask]
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
            # The ask goes into the history TRIMMED: one line naming the range,
            # so the reply under it stays readable and nothing else is re-sent.
            history.extend(
                [
                    {"role": "user", "content": summary_line},
                    {"role": "assistant", "content": reply},
                ]
            )
            result = parsed
            break
        if result is None:
            error = f"turn {turn + 1} failed after all {attempts} attempt(s)"
            break
        if result.done:
            logging.info("director: done after %d turn(s)", turn + 1)
            break
        # No separate record for the playback: it is the next turn's ask, and
        # that ask is recorded in full.  Writing it twice doubled the report.
        previous = result
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
    final = order_segments(view, state.order) if state.order else None
    final_order = normalise(final, counts) if final else seed
    outcome = LLM_ERROR if error else OK
    recorder.flush_unit(unit, outcome=outcome, reason=error)
    if error:
        logger.error("director: %s", error)
    return ConversationResult(
        ops=ops_by_stem,
        order=final_order,
        plan=state.plan,
        ok=not error,
        error=error,
        reviewed_through=state.reviewed_through,
        turns=state.turns,
    )
