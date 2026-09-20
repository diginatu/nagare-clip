"""One conversation over the whole video: what to ask next, what a reply does.

The director used to get one call per segment, each an independent shot at a
stretch of footage it could never revisit.  Here it is one conversation: the
whole video is numbered once (:mod:`nagare_clip.director.display`), and each
turn the code asks for an APPROXIMATE range — "around lines 41 to 80" — takes
back a JSON object of ops, and asks for the next.

Two rules carry the design:

* **Approximate, never a hard boundary.**  The plan stage's line ranges were
  hard, and the director copied them straight into its op boundaries — a
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
    "rewrite it. Op line numbers are this transcript's numbers."
)


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
    refusal: str | None = None
    error: str | None = None
    #: The segments this reply is about: the ones its range covers, plus the
    #: ones its ops landed in.  The caller previews exactly these back, and it
    #: cannot work them out from *ops* alone — those are source coordinates,
    #: and one source may play as several segments.  A range with no ops still
    #: counts: it owns that stretch, so what plays there just changed.
    segments: tuple[int, ...] = ()


def next_request(view: DisplayView, state: LoopState, chunk_lines: int) -> str:
    """The user message for the next turn.

    The range is deliberately vague — "around lines X to Y" — and X is the line
    after the one the MODEL said it reviewed through, never the end of the
    range the code asked for last time.
    """
    total = len(view.lines)
    start = state.reviewed_through + 1
    if start > total:
        return (
            f"Every line (1-{total}) has been reviewed. "
            'Reply {"done": true} to finish, or send one more range to rewrite '
            "a stretch you want to change.\n\n" + REPLY_SHAPE
        )
    end = min(start + max(chunk_lines, 1) - 1, total)
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

    if data.get("done") is True:
        state.turns += 1
        if state.reviewed_through >= total:
            return ReplyResult(done=True)
        unreviewed = state.reviewed_through + 1
        return ReplyResult(
            refusal=(
                f"not done: lines {unreviewed}-{total} have not been reviewed "
                f"yet. Continue from line {unreviewed}."
            )
        )

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

    accepted: list[tuple[int, DirectorOp]] = []
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
                )
            )

    # The reply OWNS its range: what started inside it goes, then the new ops
    # land.  In that order — otherwise a reply would delete its own ops.
    for index in list(state.ops):
        kept = [op for op in state.ops[index] if not _starts_inside(view, index, op, *span)]
        if kept:
            state.ops[index] = kept
        else:
            del state.ops[index]
    for index, op in accepted:
        state.ops.setdefault(index, []).append(op)
    for index in state.ops:
        state.ops[index].sort(key=_sort_key)

    state.reviewed_through = max(state.reviewed_through, reviewed)
    state.turns += 1
    touched = {index for index, _op in accepted}
    touched.update(
        line.segment for n in range(span[0], span[1] + 1) if (line := view.line(n)) is not None
    )
    return ReplyResult(
        ops=[op for _index, op in accepted],
        drops=drops,
        refusal="\n".join(refusals) if refusals else None,
        segments=tuple(sorted(touched)),
    )
