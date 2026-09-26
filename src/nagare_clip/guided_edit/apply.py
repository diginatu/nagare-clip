"""guided_edit apply orchestration (Pass B2).

Span ops (``cut``/``speed``/``keep``) are a pure whole-line-range wrap — the
director already fixed the boundaries, so they are applied deterministically
with no LLM call (see :func:`apply_span_op`); ``overlay`` is a single point
marker carrying its own duration (:func:`apply_point_op`).  Only ``edit``
ops, which need a within-line ``{{old->new}}`` the director only described in
prose, go through a small-LLM call over the op's boundary line(s); the result
is spliced back into the verbatim edit lines.  Every op is verified via
:mod:`reconcile` and a failing one is reverted and recorded, so a forgotten
closing tag or a silent rephrase never corrupts the file.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.edit_lines import EditFile, parse_edit_lines
from nagare_clip.guided_edit.reconcile import verify_op
from nagare_clip.intervals.sync_json import (
    _SPEED_OPEN_RE,
    OVERLAY_TAG_RE,
    SPEED_TAG_RE,
    escape_overlay_text,
)
from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    VERIFY_FAIL,
    Recorder,
)
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts
from nagare_clip.text_filter.llm_filter import _call_llm

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, str]], dict[str, Any]], str]
Unapplied = tuple[DirectorOp, str]

_LINE_RE = re.compile(r"^\s*(\d+):\s?(.*)$")


def _open_close_counts(line: str, op_type: str) -> tuple[int, int]:
    """(#open tags, #close tags) of *op_type* on a single line.

    An ``<overlay/>`` marker is self-closing, so it counts as both — it marks
    its own line as occupied without opening a span over the lines below.
    """
    if op_type == "speed":
        total = len(SPEED_TAG_RE.findall(line))
        opens = len(_SPEED_OPEN_RE.findall(line))
        return opens, total - opens
    if op_type == "overlay":
        marks = len(OVERLAY_TAG_RE.findall(line))
        return marks, marks
    if op_type == "keep":
        return line.count("<keep>"), line.count("</keep>")
    if op_type == "cut":
        return line.count("<cut>"), line.count("</cut>")
    return 0, 0  # pragma: no cover


def occupied_lines(lines: list[str], op_type: str) -> set[int]:
    """1-based line numbers already touched by a same-type span.

    A line is occupied if it lies inside an open same-type span or carries any
    same-type tag itself — so wrapping it again would nest/interleave with the
    existing span (which the downstream extractors reject).
    """
    occ: set[int] = set()
    depth = 0
    # A silence line's bracket is opaque: only the markers around it count.
    for i, line in enumerate(parse_edit_lines(lines).marker_text(), start=1):
        opens, closes = _open_close_counts(line, op_type)
        if depth > 0 or opens or closes:
            occ.add(i)
        depth = max(0, depth + opens - closes)
    return occ


def blocked_lines(lines: list[str], op_type: str) -> set[int]:
    """1-based line numbers an op of *op_type* may not touch.

    Same-type overlap would nest/interleave tags (rejected downstream).
    ``<cut>`` is destructive on top of that: the intervals stage deletes
    whatever it wraps, silently swallowing any other tag caught inside — so a
    cut op is blocked by lines under ANY existing tag, and every op is blocked
    by lines under an existing ``<cut>`` span.
    """
    blocked: set[int] = set()
    for t in blocking_types(op_type):
        blocked |= occupied_lines(lines, t)
    return blocked


def blocking_types(op_type: str) -> tuple[str, ...]:
    """The marker types whose lines an op of *op_type* may not touch."""
    if op_type == "cut":
        return ("cut", "keep", "speed", "overlay")
    return (op_type, "cut")


def clip_range(a: int, b: int, occupied: set[int]) -> tuple[int, int] | None:
    """Largest contiguous run of free lines within ``[a, b]`` (ties → earliest),
    or ``None`` if every line is occupied."""
    best: tuple[int, int] | None = None
    start: int | None = None
    for n in range(a, b + 2):  # +2 so a trailing run is flushed on the last pass
        if n <= b and n not in occupied:
            if start is None:
                start = n
        elif start is not None:
            run = (start, n - 1)
            if best is None or (run[1] - run[0]) > (best[1] - best[0]):
                best = run
            start = None
    return best


def span_op_order(ops: list[DirectorOp]) -> list[int]:
    """Indices of *ops* in the order they are applied.

    Cut ops go after everything else: ``<cut>`` deletes whatever it wraps, so
    protective/annotation markers (keep/speed/overlay) must land first and the
    cut then clips around them (the director may emit overlapping ops).
    """
    return sorted(range(len(ops)), key=lambda i: (ops[i].type == "cut", i))


@dataclass(frozen=True)
class SpanPlacement:
    """Where one span/point op actually lands, given the markers already placed.

    ``lines`` is the effective range (``None`` when it fully overlaps existing
    markers), ``candidate`` the edit lines with it applied, and ``reason`` why
    it was not applied (``None`` = applied).
    """

    lines: tuple[int, int] | None
    candidate: list[str] | None = None
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.reason is None


def to_physical(parsed: EditFile, op: DirectorOp) -> DirectorOp | str:
    """*op* re-addressed to physical ``_edits.txt`` lines, or why it cannot be.

    A plain edge is its speech line's physical line; a ``"n~"`` edge is the
    silence line after ``n`` — an ordinary line index like any other, so every
    op lands as a marker.  An overlay is a point: only its start is mapped.  A
    ``"n~"`` whose silence has no line (shorter than
    ``director.silence_line_min``, or no gap at all) is not guessed at.
    """
    if op.type == "edit" and (op.gap_start or op.gap_end):
        return "edit op cannot address a silence (it has no words to patch)"
    ends = [(op.lines[0], op.gap_start), (op.lines[1], op.gap_end)]
    mapped: list[int] = []
    for line, gap in ends:
        index = parsed.file_index(line, gap)
        if index is None:
            if gap:
                return (
                    f"{op.type} op addresses the silence after line {line}, which has no "
                    "silence line (shorter than director.silence_line_min, or no gap)"
                )
            return f"{op.type} op: line {line} is not in the file"
        mapped.append(index)
    return replace(op, lines=(mapped[0], mapped[1]), gap_start=False, gap_end=False)


def _trim_to_own_edges(
    clipped: tuple[int, int], op: DirectorOp, silence_lines: set[int]
) -> tuple[int, int] | None:
    """Move a clipped edge off a silence line the op did not address.

    Silence lines inside a span are simply part of it, but a clip must not
    leave an edge ON one the op did not ask for: that would keep (or cut) the
    whole wait instead of stopping at the words, as the edge did before
    silence lines existed.  An overlay point likewise lands on a speech line
    unless its own start is the silence.
    """
    a, b = clipped
    if op.type == "overlay":
        for x in range(a, b + 1):
            if x == op.lines[0] or x not in silence_lines:
                return (x, x)
        return None
    while a <= b and a != op.lines[0] and a in silence_lines:
        a += 1
    while b >= a and b != op.lines[1] and b in silence_lines:
        b -= 1
    return (a, b) if a <= b else None


def place_span_op(lines: list[str], op: DirectorOp) -> SpanPlacement:
    """Clip one ``cut``/``keep``/``speed``/``overlay`` op and apply it.

    The one deterministic clip decision: :func:`apply_ops` and the director's
    playback preview both call it, so what the preview reports is what lands.
    """
    blocked = blocked_lines(lines, op.type)
    clipped = clip_range(op.lines[0], op.lines[1], blocked)
    if clipped is not None:
        silence_lines = {slot.file_line for slot in parse_edit_lines(lines).silences()}
        clipped = _trim_to_own_edges(clipped, op, silence_lines)
    if clipped is None:
        return SpanPlacement(None, reason=f"{op.type} op fully overlaps existing span(s)")
    if op.type == "overlay":
        # An overlay is a point: only the first free line matters, and the rest
        # of the director's range is not a clip.
        clipped = (clipped[0], clipped[0])
    eff_op = replace(op, lines=clipped) if clipped != tuple(op.lines) else op
    candidate = (
        apply_point_op(lines, eff_op) if eff_op.type == "overlay" else apply_span_op(lines, eff_op)
    )
    return SpanPlacement(clipped, candidate, verify_op(lines, candidate, eff_op))


def resolve_span_ops(lines: list[str], ops: list[DirectorOp]) -> dict[int, SpanPlacement]:
    """Every non-``edit`` op's placement, applied in :func:`span_op_order`.

    Exactly the span half of :func:`apply_ops`, with no LLM and no recorder:
    ``edit`` ops only add ``{{old->new}}`` patches, which no clip reads.  Ops
    are mapped to physical lines the same way (:func:`to_physical`), so the
    placements' ``lines`` are PHYSICAL lines of *lines*.
    """
    placements: dict[int, SpanPlacement] = {}
    parsed = parse_edit_lines(lines)
    for i in span_op_order(ops):
        op = ops[i]
        if op.type == "edit":
            continue
        if op.type == "timelapse":
            placements[i] = SpanPlacement(None, reason=UNEXPANDED_TIMELAPSE)
            continue
        mapped = to_physical(parsed, op)
        if isinstance(mapped, str):
            placements[i] = SpanPlacement(None, reason=mapped)
            continue
        placed = place_span_op(lines, mapped)
        placements[i] = placed
        if placed.applied:
            lines = placed.candidate
    return placements


UNEXPANDED_TIMELAPSE = "timelapse op reached apply_ops unexpanded"


def _edge(op: DirectorOp, side: int) -> str:
    """One end of *op* as ``_director.json`` writes it (``53`` or ``"53~"``)."""
    gap = op.gap_start if side == 0 else op.gap_end
    return f"{op.lines[side]}~" if gap else str(op.lines[side])


def _span_tags(op: DirectorOp) -> tuple[str, str]:
    """(open, close) marker pair for a span op type."""
    if op.type == "cut":
        return "<cut>", "</cut>"
    if op.type == "keep":
        return "<keep>", "</keep>"
    if op.type == "speed":
        return f'<speed factor="{op.factor}">', "</speed>"
    raise ValueError(f"not a span op: {op.type}")  # pragma: no cover


def apply_span_op(lines: list[str], op: DirectorOp) -> list[str]:
    """Wrap the op's line range in its marker pair, deterministically.

    The director already fixed the boundaries and the op granularity is
    whole-line, so this is a pure splice — the open tag prepends the first
    boundary line, the close tag appends the last (both on one line when the
    range is a single line).  Existing markers/patches on the line are left
    intact (nested inside), so the underlying transcript text is unchanged.
    """
    a, b = op.lines
    open_t, close_t = _span_tags(op)
    new = list(lines)
    if a == b:
        new[a - 1] = f"{open_t}{new[a - 1]}{close_t}"
    else:
        new[a - 1] = f"{open_t}{new[a - 1]}"
        new[b - 1] = f"{new[b - 1]}{close_t}"
    return new


def apply_point_op(lines: list[str], op: DirectorOp) -> list[str]:
    """Insert an overlay's point marker at the start of its (single) line.

    An overlay has no closing tag: the marker's position is the start and its
    ``duration`` attribute — seconds of the edited timeline — is how long the
    text stays on screen.  Nothing about its on-screen time depends on where a
    second tag lands, which is the whole reason the marker is a point.

    The caption text is escaped: a multi-line caption is legitimate (Blender
    renders the break), but an edit line maps 1:1 to a WhisperX segment, so the
    break must not reach the file as a raw newline.
    """
    a = op.lines[0]
    new = list(lines)
    text = escape_overlay_text(op.text or "")
    new[a - 1] = f'<overlay text="{text}" duration="{op.duration}"/>{new[a - 1]}'
    return new


def _instruction(op: DirectorOp) -> str:
    if op.type == "cut":
        what = "Cut (delete) the span described below by wrapping it in <cut>...</cut>."
    elif op.type == "speed":
        what = f'Speed up the span by wrapping it in <speed factor="{op.factor}">...</speed>.'
    elif op.type == "overlay":
        what = (
            f"Add an on-screen overlay by inserting <overlay "
            f'text="{escape_overlay_text(op.text or "")}" duration="{op.duration}"/> '
            f"at the position described."
        )
    elif op.type == "keep":
        what = "Protect the span from being cut by wrapping it in <keep>...</keep>."
    elif op.type == "edit":
        what = "Make the within-line text edit described below using {{old->new}}."
    else:  # pragma: no cover - parse layer rejects unknown types
        what = "Edit the span."
    note = f" Where: {op.note}" if op.note else ""
    return what + note


def build_user_prompt(op: DirectorOp, lines: list[str]) -> str:
    """Numbered boundary line(s) for the op, with the middle omitted for wide
    ranges (the tag's open/close on the boundaries spans them automatically)."""
    a, b = op.lines
    parts = [_instruction(op), "", "Lines:"]
    if a == b:
        parts.append(f"{a}: {lines[a - 1]}")
    else:
        parts.append(f"{a}: {lines[a - 1]}")
        if b - a > 1:
            parts.append(f"... ({b - a - 1} line(s) in between are included automatically) ...")
        parts.append(f"{b}: {lines[b - 1]}")
    return "\n".join(parts)


def _parse_returned_lines(response: str, allowed: set[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for raw in response.splitlines():
        m = _LINE_RE.match(raw)
        if m:
            n = int(m.group(1))
            if n in allowed:
                out[n] = m.group(2)
    return out


def _build_messages(op: DirectorOp, lines: list[str], cfg: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": build_user_prompt(op, lines)},
    ]


def _apply_one_op(
    lines: list[str],
    op: DirectorOp,
    cfg: dict[str, Any],
    call_llm: CallLLM,
    messages: list[dict[str, str]],
) -> tuple[list[str], str]:
    """Returns (new_lines, raw_response)."""
    a, b = op.lines
    allowed = {a} if a == b else {a, b}
    response = call_llm(messages, cfg)
    returned = _parse_returned_lines(response, allowed)
    new_lines = list(lines)
    for n, text in returned.items():
        new_lines[n - 1] = text
    return new_lines, response


def apply_ops(
    edit_lines: list[str],
    ops: list[DirectorOp],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "guided_edit",
) -> tuple[list[str], list[Unapplied]]:
    """Apply ops sequentially; return (new_lines, unapplied).

    Span ops are wrapped deterministically (no LLM, no retry) and verified;
    ``edit`` ops call the LLM and, if the result altered the underlying text or
    was not reflected, retry (config ``max_retries``) with a nudged temperature.
    Any op that still fails verification is reverted and added to *unapplied*
    with the last failure reason.
    """
    lines = list(edit_lines)
    # The silence lines are fixed before any op lands, so one parse maps every
    # op; ops keep their SOURCE coordinates for the report and "unapplied".
    parsed = parse_edit_lines(lines)
    unapplied: list[Unapplied] = []
    recorder.begin(unit)
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    for i in span_op_order(ops):
        source_op = ops[i]
        section = f"op {i}: {source_op.type} [{_edge(source_op, 0)}-{_edge(source_op, 1)}]"
        mapped = to_physical(parsed, source_op)
        if isinstance(mapped, str) and source_op.type != "timelapse":
            recorder.attempt(
                unit=unit,
                attempt=0,
                total=1,
                messages=[],
                outcome=VERIFY_FAIL,
                reason=mapped,
                cfg=None,
                deterministic=True,
                section=section,
            )
            logger.warning("guided_edit: op %s dropped: %s", source_op.type, mapped)
            unapplied.append((source_op, mapped))
            continue
        op = source_op if isinstance(mapped, str) else mapped
        if op.type == "timelapse":
            # run_guided_edit desugars these before we see them (see
            # guided_edit.timelapse); reaching here means a caller skipped that
            # step, and _span_tags has no marker pair for the type.
            reason = UNEXPANDED_TIMELAPSE
            recorder.attempt(
                unit=unit,
                attempt=0,
                total=1,
                messages=[],
                outcome=VERIFY_FAIL,
                reason=reason,
                cfg=None,
                deterministic=True,
                section=section,
            )
            logger.warning("guided_edit: op %s dropped: %s", op.type, reason)
            unapplied.append((source_op, reason))
            continue
        if op.type != "edit":
            # Span ops are a pure line-range wrap — no LLM judgement needed.
            # Clip the range to lines it may touch (see blocked_lines) so the
            # tags stay disjoint where the downstream extractors require it.
            placed = place_span_op(lines, op)
            clipped = placed.lines
            if clipped is None:
                reason = placed.reason or ""
                recorder.attempt(
                    unit=unit,
                    attempt=0,
                    total=1,
                    messages=[],
                    outcome=VERIFY_FAIL,
                    reason=reason,
                    cfg=None,
                    deterministic=True,
                    section=section,
                )
                logger.warning("guided_edit: op %s dropped: %s", op.type, reason)
                unapplied.append((source_op, reason))
                continue
            if op.type == "overlay":
                moved = clipped[0] != op.lines[0]
            else:
                moved = clipped != tuple(op.lines)
            if moved:
                logger.warning(
                    "guided_edit: op %s clipped from %s to %s (span overlap)",
                    op.type,
                    tuple(op.lines),
                    clipped,
                )
            reason = placed.reason
            recorder.attempt(
                unit=unit,
                attempt=0,
                total=1,
                messages=[],
                response="\n".join(placed.candidate[clipped[0] - 1 : clipped[1]]),
                outcome=OK if reason is None else VERIFY_FAIL,
                reason="" if reason is None else reason,
                cfg=None,
                deterministic=True,
                section=section,
            )
            if placed.applied:
                lines = placed.candidate
            else:
                unapplied.append((source_op, reason))
            continue
        last_reason = f"{op.type} op not applied"
        applied = False
        for attempt in range(attempts):
            attempt_cfg = cfg_for_attempt(cfg, attempt)
            messages = _build_messages(op, lines, attempt_cfg)
            try:
                candidate, response = _apply_one_op(lines, op, attempt_cfg, call_llm, messages)
            except Exception as e:  # noqa: BLE001 - LLM/parse failures are recoverable
                last_reason = f"LLM/apply error: {e}"
                logger.warning(
                    "guided_edit: op %s attempt %d/%d failed: %s",
                    op.type,
                    attempt + 1,
                    attempts,
                    e,
                )
                recorder.attempt(
                    unit=unit,
                    attempt=attempt,
                    total=attempts,
                    messages=messages,
                    error=str(e),
                    outcome=LLM_ERROR,
                    reason=last_reason,
                    cfg=attempt_cfg,
                    section=section,
                )
                continue
            reason = verify_op(lines, candidate, op)
            if reason is None:
                recorder.attempt(
                    unit=unit,
                    attempt=attempt,
                    total=attempts,
                    messages=messages,
                    response=response,
                    outcome=OK,
                    cfg=attempt_cfg,
                    section=section,
                )
                lines = candidate
                applied = True
                break
            last_reason = reason
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=messages,
                response=response,
                outcome=VERIFY_FAIL,
                reason=reason,
                cfg=attempt_cfg,
                section=section,
            )
            logger.warning(
                "guided_edit: op %s attempt %d/%d reverted: %s",
                op.type,
                attempt + 1,
                attempts,
                reason,
            )
        if not applied:
            unapplied.append((source_op, last_reason))
    if unapplied:
        outcome = DROPPED_ITEMS
        reason = f"{len(unapplied)}/{len(ops)} op(s) unapplied"
    else:
        outcome = OK
        reason = f"{len(ops)} op(s) applied"
    recorder.flush_unit(unit, outcome=outcome, reason=reason)
    return lines, unapplied
