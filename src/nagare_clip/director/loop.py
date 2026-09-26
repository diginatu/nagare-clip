"""One conversation over the whole video: what to ask next, what a reply does.

The director used to get one call per segment, each an independent shot at a
stretch of footage it could never revisit.  Here it is one conversation: the
whole video is numbered once (:mod:`nagare_clip.director.display`), and each
turn the code asks for an APPROXIMATE range — "around lines 41 to 80" — takes
back a JSON object of ops, and asks for the next.

Two rules carry the design:

* **Approximate, never a hard boundary.**  The (since removed) plan stage's
  line ranges were hard, and the director copied them straight into its op boundaries — a
  timelapse that opened on the line announcing the work.  The range here is a
  suggestion; the model says where it actually stopped
  (``reviewed_through``) and the next request starts after THAT line.
* **A reply owns a range.**  Every op already accepted whose first line falls
  inside the reply's ``range`` is replaced by the reply's ops.  Rewriting an
  earlier range is therefore the same operation as a first pass over it, and
  nothing outside the range is disturbed — which is what makes the playback
  preview worth answering: the model can act on what it reads.

``done`` while lines remain unreviewed is refused, naming the first unreviewed
line, and the loop continues.  A range crossing a segment join is two pieces of
unrelated footage: a ``cut`` or ``keep`` is split at the join (both halves mean
exactly what they meant), anything else is refused with the join's line number
for the model to split itself.  Ops go through the existing parser, so
``max_keep_lines``, the timelapse factor floor and the off-menu types drop
exactly as they always did, and their drop messages reach the caller.

A reply may also carry ``order``: the playback order, as display ranges in the
order they play.  The view never changes shape — it is numbered in shooting
order once — so the order is state like the ops, replaced whole by the last
reply that sent one.  A range may start or end on a silence line (the silence
plays where its range plays); a range over a source boundary is two segments
that happen to be adjacent.  An order that does not tile the video, or whose
boundary would split a timelapse, is refused and the previous one stays; a new
timelapse across a boundary of the order in force is refused like one across a
source boundary.

The conversation opens with a PLANNING turn: the first ask is for a plan in
prose (``{"plan": "...", "order": ...}``), and a first reply without one is
unusable.  The plan is state like the order — replaced whole by any later reply
that sends ``"plan"`` — and every turn's edit state leads with the plan in
force, so a plan the ops drifted from stays in front of the one who can fix
either.

Nothing here raises on a bad reply: a malformed one comes back as
:attr:`ReplyResult.error`, for the caller's retry ladder.

Pure: no I/O, no LLM — the caller injects ``call_llm``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from nagare_clip.director.director_llm import (
    DirectorOp,
    strip_code_fence,
    try_parse_director_response,
)
from nagare_clip.director.display import DisplayView
from nagare_clip.order import Segment

#: Op types whose meaning survives being cut in two at a segment join: each
#: half still removes / protects exactly the footage it names.  A timelapse or
#: an overlay does not (one speed-up over two unrelated shots, one caption in
#: two places), so those are refused for the model to split deliberately.
SPLITTABLE = frozenset({"cut", "keep"})

REPLY_SHAPE = (
    'Reply with one JSON object and nothing else: {"range": [first, last], '
    '"reviewed_through": <the last line you actually reviewed>, "ops": [...]}. '
    '"range" is the stretch this reply owns: every op accepted earlier whose '
    "first line falls inside it is REPLACED by the ops below, and nothing "
    "outside it changes — so re-sending a range you already covered is how you "
    "rewrite it. Op line numbers are this transcript's numbers. "
    'Optionally add "order": [[first, last], ...] and/or "plan": "..."; either '
    "alone changes nothing else."
)

#: A display range, 1-based inclusive.
Range = tuple[int, int]


@dataclass
class LoopState:
    """What the conversation has accumulated so far.

    *ops* are per-source ops (the coordinates ``_director.json`` speaks), keyed
    by the segment's 1-based index in the playback order — not by stem: one
    source can play as several segments, and an op belongs to one of them.
    """

    ops: dict[int, list[DirectorOp]] = field(default_factory=dict)
    reviewed_through: int = 0
    turns: int = 0
    #: The playback order in force, as display ranges; empty = shooting order.
    order: list[Range] = field(default_factory=list)
    #: The plan in force, in the model's own words; empty until the first turn.
    plan: str = ""


@dataclass(frozen=True)
class ReplyResult:
    """What one reply did.

    *ops* are the ones accepted this turn, already in source coordinates.
    *done* is True only when the model's ``done`` was ACCEPTED; a ``done`` with
    lines still unreviewed comes back as a *refusal* instead, and the loop goes
    on.  *error* is a malformed reply, for the retry ladder.
    """

    ops: list[DirectorOp] = field(default_factory=list)
    drops: list[str] = field(default_factory=list)
    done: bool = False
    #: True when this reply's ``order`` was accepted.
    reordered: bool = False
    refusal: str | None = None
    error: str | None = None


#: The planning turn's ask.  Sent once; later turns carry only the one-line
#: history summary.  What a plan covers is listed here rather than in the
#: prompt, which only has to say that the plan exists and stays revisable.
PLAN_REQUEST = (
    "Before any op, write your plan for the whole video. Read all of it first. "
    "In prose, say: the throughline — what this video is about and what the "
    "viewer should come away with; what to cut and what to compress, roughly "
    "where (approximate line numbers are fine — they mark sections, not op "
    "boundaries); the moments worth a caption; the order, and why, if it should "
    "not be shooting order; and the runtime you expect against the brief.\n\n"
    'Reply with one JSON object and nothing else: {"plan": "<your plan>"}, '
    'optionally with "order": [[first, last], ...]. No ops yet: the next turns '
    'ask for them range by range, and any later reply may send "plan" again to '
    "replace this one."
)

PLAN_SUMMARY = "Write the plan."


def _next_span(view: DisplayView, state: LoopState, chunk_lines: int) -> tuple[int, int] | None:
    """The lines the next turn is asked for; ``None`` once none are left.

    The start is the line after the one the MODEL said it reviewed through,
    never the end of the range the code asked for last time.
    """
    total = len(view.lines)
    start = state.reviewed_through + 1
    if start > total:
        return None
    return (start, min(start + max(chunk_lines, 1) - 1, total))


def request_summary(view: DisplayView, state: LoopState, chunk_lines: int) -> str:
    """The one line an ask is trimmed to once it is no longer the live turn.

    The conversation keeps the model's own replies — which turn made which op,
    what it rewrote — and a reply is unreadable without the ask it answered.
    But only the RANGE of that ask is needed to read it: the approximation
    wording and the reply shape are in the system prompt and in the live
    request, and the state block that ask carried is stale the moment the next
    turn recomputes it.  Repeating either through the history is uncached
    tokens on every turn for nothing.
    """
    if not state.plan:
        return PLAN_SUMMARY
    span = _next_span(view, state, chunk_lines)
    if span is None:
        return "Every line has been reviewed."
    return f"Review around lines {span[0]} to {span[1]}."


def next_request(view: DisplayView, state: LoopState, chunk_lines: int) -> str:
    """The live user message for the next turn.

    The range is deliberately vague — "around lines X to Y" — and X is the line
    after the one the MODEL said it reviewed through, never the end of the
    range the code asked for last time.
    """
    total = len(view.lines)
    if not state.plan:
        return PLAN_REQUEST
    span = _next_span(view, state, chunk_lines)
    if span is None:
        return (
            f"Every line (1-{total}) has been reviewed. "
            'Reply {"done": true} to finish, or send one more range to rewrite '
            "a stretch you want to change.\n\n" + REPLY_SHAPE
        )
    start, end = span
    return (
        f"Reviewed through line {state.reviewed_through} of {total}. "
        f"Next, review around lines {start} to {end} — approximately: stop "
        "where the action ends, a few lines earlier or later, wherever the "
        f"footage actually breaks. Do not stop at line {end} because it was "
        "asked for. Emit the ops for the stretch you really reviewed, and say "
        "which line you reviewed through.\n\n" + REPLY_SHAPE
    )


def _joins(view: DisplayView, first: int, last: int) -> list[int]:
    """The display lines inside ``[first, last)`` a segment join follows."""
    return [n for n in range(first, last) if view.segment_join_after(n)]


def _pieces(view: DisplayView, first: int, last: int) -> list[tuple[int, int]]:
    """*[first, last]* cut into one range per segment it touches."""
    out: list[tuple[int, int]] = []
    start = first
    for join in _joins(view, first, last):
        out.append((start, join))
        start = join + 1
    out.append((start, last))
    return out


def _int(value: Any) -> int | None:
    return None if isinstance(value, bool) or not isinstance(value, int) else value


def _read_range(value: Any, total: int) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    first, last = _int(value[0]), _int(value[1])
    if first is None or last is None or not 1 <= first <= last <= total:
        return None
    return (first, last)


def _starts_inside(view: DisplayView, segment: int, op: DirectorOp, first: int, last: int) -> bool:
    """Does *op*'s first line fall in the display range ``[first, last]``?"""
    number = view.from_source(segment, op.lines[0], silence=op.gap_start)
    return number is not None and first <= number <= last


def _sort_key(op: DirectorOp) -> tuple[int, bool, int, bool]:
    return (op.lines[0], op.gap_start, op.lines[1], op.gap_end)


def _read_order(value: Any, total: int) -> tuple[list[Range] | None, str]:
    """The reply's ``order`` as ranges, or ``(None, why)``."""
    if not isinstance(value, list) or not value:
        return None, f'"order" must be a non-empty list of [first, last] ranges of 1-{total}.'
    ranges: list[Range] = []
    for item in value:
        span = _read_range(item, total)
        if span is None:
            return None, f'"order" entry {item!r} is not a [first, last] range of 1-{total}.'
        ranges.append(span)
    problems: list[str] = []
    cursor = 1
    for first, last in sorted(ranges):
        if first > cursor:
            problems.append(f"lines {cursor}-{first - 1} are in no range")
        elif first < cursor:
            problems.append(f"lines {first}-{min(last, cursor - 1)} are in more than one range")
        cursor = max(cursor, last + 1)
    if cursor <= total:
        problems.append(f"lines {cursor}-{total} are in no range")
    if problems:
        return None, (
            '"order" must cover every line exactly once — use cut to drop footage: '
            + "; ".join(problems)
            + "."
        )
    return ranges, ""


def order_breaks(order: list[Range], total: int) -> set[int]:
    """Display lines *y* after which the playback jumps elsewhere.

    A range ending at *y* is a break unless the range that plays next starts
    at ``y + 1`` — two ranges played back to back in view order are one run of
    footage.  The last line of the video is never a break.
    """
    breaks: set[int] = set()
    for i, (_first, last) in enumerate(order):
        if last >= total:
            continue
        following = order[i + 1][0] if i + 1 < len(order) else None
        if following != last + 1:
            breaks.add(last)
    return breaks


def order_segments(view: DisplayView, order: list[Range]) -> list[Segment] | None:
    """*order* in source coordinates, split at source boundaries.

    A range's first line is the speech line itself, or — for a silence line —
    the line after it (the silence before a line is its segment's by default).
    Its last line is a speech line, or a silence line, which becomes that line
    ``~`` (:attr:`~nagare_clip.order.Segment.gap_end`).  ``None`` when a piece
    holds no speech line at all.  Not normalised: the caller knows the counts.
    """
    out: list[Segment] = []
    for first, last in order:
        for a, b in _pieces(view, first, last):
            start, end = view.line(a), view.line(b)
            if start is None or end is None:  # pragma: no cover - ranges are validated
                return None
            begin = start.source_line + 1 if start.is_silence else start.source_line
            if begin > end.source_line:
                return None
            out.append(Segment(start.stem, (begin, end.source_line), end.is_silence))
    return out


def order_ranges(view: DisplayView, segments: list[Segment]) -> list[Range]:
    """Source-coordinate *segments* as display ranges, or ``[]`` if they don't map.

    The inverse of :func:`order_segments`, for seeding the conversation with
    an ``order.json``.  A segment starting at ``a`` takes the silence line
    before ``a`` unless another segment ends on it (``a-1~``).  Anything that
    does not tile the view comes back ``[]`` — shooting order.
    """
    claimed = {(s.stem, s.lines[1]) for s in segments if s.lines is not None and s.gap_end}
    by_stem: dict[str, list] = {}
    for seg in view.segments:
        by_stem.setdefault(seg.stem, []).append(seg)
    out: list[Range] = []
    for seg in segments:
        blocks = by_stem.get(seg.stem, [])
        if not blocks:
            return []
        if seg.lines is None:
            if len(blocks) != 1:
                return []
            out.append((blocks[0].first, blocks[0].last))
            continue
        a, b = seg.lines
        x = y = None
        for block in blocks:
            if a > 1 and (seg.stem, a - 1) not in claimed:
                x = x or view.from_source(block.index, a - 1, silence=True)
            x = x or view.from_source(block.index, a)
            if seg.gap_end:
                y = y or view.from_source(block.index, b, silence=True)
            y = y or view.from_source(block.index, b)
        if x is None or y is None or x > y:
            return []
        out.append((x, y))
    ranges, _why = _read_order(out, len(view.lines))
    return ranges or []


def _display_range(view: DisplayView, segment: int, op: DirectorOp) -> Range | None:
    first = view.from_source(segment, op.lines[0], silence=op.gap_start)
    last = view.from_source(segment, op.lines[1], silence=op.gap_end)
    return None if first is None or last is None else (first, last)


def _split_by(span: Range, breaks: set[int]) -> int | None:
    """The first break strictly inside *span*, if any."""
    return next((y for y in sorted(breaks) if span[0] <= y < span[1]), None)


def apply_reply(
    view: DisplayView,
    state: LoopState,
    reply: str,
    *,
    max_keep_lines: int = 0,
) -> ReplyResult:
    """Apply one reply to *state*, and say what it did.  Never raises."""
    total = len(view.lines)
    try:
        data = json.loads(strip_code_fence(reply))
    except (ValueError, TypeError):
        return ReplyResult(error="the reply is not JSON. " + REPLY_SHAPE)
    if not isinstance(data, dict):
        return ReplyResult(error="the reply is not a JSON object. " + REPLY_SHAPE)

    plan: str | None = None
    if "plan" in data:
        raw = data["plan"]
        if not isinstance(raw, str) or not raw.strip():
            return ReplyResult(error='"plan" must be your plan as non-empty text.')
        plan = raw.strip()
    elif not state.plan:
        return ReplyResult(
            error='there is no plan yet: reply {"plan": "<your plan>"} first, before any op.'
        )

    if data.get("done") is True:
        state.turns += 1
        if plan is not None:
            state.plan = plan
        reordered = False
        if "order" in data:
            # Never silently dropped: a refused order is not a finished edit.
            refusal = _apply_order(view, state, data["order"], state.ops)
            if refusal:
                return ReplyResult(refusal=refusal)
            reordered = True
        if state.reviewed_through >= total:
            return ReplyResult(done=True, reordered=reordered)
        unreviewed = state.reviewed_through + 1
        return ReplyResult(
            reordered=reordered,
            refusal=(
                f"not done: lines {unreviewed}-{total} have not been reviewed "
                f"yet. Continue from line {unreviewed}."
            ),
        )

    if "range" not in data and ("order" in data or plan is not None):
        if data.get("ops"):
            return ReplyResult(
                error='the reply has "ops" but no "range" for them to own. ' + REPLY_SHAPE
            )
        state.turns += 1
        if plan is not None:
            state.plan = plan
        if "order" not in data:
            return ReplyResult()
        refusal = _apply_order(view, state, data["order"], state.ops)
        return ReplyResult(refusal=refusal, reordered=refusal is None)
    if "range" not in data:
        return ReplyResult(
            error='the reply has no "range" and is not {"done": true}. ' + REPLY_SHAPE
        )
    span = _read_range(data.get("range"), total)
    if span is None:
        return ReplyResult(
            error=f'bad "range" {data.get("range")!r}: two line numbers of 1-{total}, '
            "the first not after the second."
        )
    reviewed = _int(data.get("reviewed_through"))
    if reviewed is None or not 1 <= reviewed <= total:
        return ReplyResult(
            error=f'bad "reviewed_through" {data.get("reviewed_through")!r}: '
            f"the last line you reviewed, 1-{total}."
        )

    drops: list[str] = []
    parsed = try_parse_director_response(
        json.dumps({"ops": data.get("ops")}), total, drops, max_keep_lines, 1
    )
    if parsed is None:
        return ReplyResult(error='the reply has no "ops" array. ' + REPLY_SHAPE)

    accepted: list[tuple[int, DirectorOp, Range]] = []
    refusals: list[str] = []
    for op in parsed:
        first, last = op.lines
        if op.gap_start or op.gap_end:
            drops.append(
                f'{op.type} op lines {list(op.lines)} use the "n~" form; a silence '
                "is a numbered line of its own here — give its number."
            )
            continue
        pieces = _pieces(view, first, last)
        if len(pieces) > 1 and op.type not in SPLITTABLE:
            refusals.append(
                f"{op.type} [{first},{last}] crosses the segment join after line "
                f"{pieces[0][1]}: the two sides are different footage. Send it as "
                "separate ops, one per side."
            )
            continue
        for piece in pieces:
            mapped = view.to_source(*piece)
            line = view.line(piece[0])
            if mapped is None or line is None:  # pragma: no cover - _pieces guarantees it
                drops.append(f"{op.type} op lines {list(op.lines)} do not map to a segment")
                continue
            _stem, source_lines, gap_start, gap_end = mapped
            accepted.append(
                (
                    line.segment,
                    replace(op, lines=source_lines, gap_start=gap_start, gap_end=gap_end),
                    piece,
                )
            )

    # The reply OWNS its range: what started inside it goes, then the new ops
    # land.  In that order — otherwise a reply would delete its own ops.
    kept: dict[int, list[DirectorOp]] = {}
    for index, ops in state.ops.items():
        survivors = [op for op in ops if not _starts_inside(view, index, op, *span)]
        if survivors:
            kept[index] = survivors
    candidate: dict[int, list[DirectorOp]] = {i: list(v) for i, v in kept.items()}
    for index, op, _piece in accepted:
        candidate.setdefault(index, []).append(op)

    # The order is judged against every op the reply leaves standing, so one
    # reply can move a boundary and rewrite the timelapse it would split.
    reordered = False
    if "order" in data:
        refusal = _apply_order(view, state, data["order"], candidate)
        if refusal:
            refusals.append(refusal)
        else:
            reordered = True
    breaks = order_breaks(state.order, total)
    landed: list[tuple[int, DirectorOp]] = []
    for index, op, piece in accepted:
        split = _split_by(piece, breaks) if op.type == "timelapse" else None
        if split is not None:
            refusals.append(
                f"timelapse [{piece[0]},{piece[1]}] crosses the order's break after line "
                f"{split}: its two sides play apart. Move the break, or send one "
                "timelapse per side."
            )
            continue
        landed.append((index, op))

    state.ops = kept
    for index, op in landed:
        state.ops.setdefault(index, []).append(op)
    for index in state.ops:
        state.ops[index].sort(key=_sort_key)

    if plan is not None:
        state.plan = plan
    state.reviewed_through = max(state.reviewed_through, reviewed)
    state.turns += 1
    return ReplyResult(
        ops=[op for _index, op in landed],
        drops=drops,
        refusal="\n".join(refusals) if refusals else None,
        reordered=reordered,
    )


def _apply_order(
    view: DisplayView,
    state: LoopState,
    value: Any,
    ops: dict[int, list[DirectorOp]],
) -> str | None:
    """Replace ``state.order`` with *value*, or say why not (and keep it)."""
    total = len(view.lines)
    order, why = _read_order(value, total)
    if order is None:
        return f"order not applied: {why} The previous order stays."
    if order_segments(view, order) is None:
        return (
            "order not applied: a range holds only a silence line. A silence plays "
            "with the range that holds a line beside it. The previous order stays."
        )
    breaks = order_breaks(order, total)
    for index in sorted(ops):
        for op in ops[index]:
            if op.type != "timelapse":
                continue
            shown = _display_range(view, index, op)
            split = _split_by(shown, breaks) if shown is not None else None
            if split is not None:
                return (
                    f"order not applied: its break after line {split} splits timelapse "
                    f"[{shown[0]},{shown[1]}]. Move the break outside it, or rewrite the "
                    "timelapse in the same reply. The previous order stays."
                )
    state.order = order
    return None
