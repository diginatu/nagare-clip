"""The director's operations: the ``{stem}_director.json`` contract.

An op references source lines by 1-based number (``"n~"`` names the silence
after line ``n``; ``"0~"`` the leading silence, before line 1) and never re-outputs transcript text, except an ``edit``
op's exact ``old``/``new``.  :func:`parse_op` validates one op — the shared
check the agent skill's ``write`` tool reports and :func:`ops_from_dict`
(every reader of a hand-editable file) drops by.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from nagare_clip.guided_edit.patch import forbidden_problem

logger = logging.getLogger(__name__)

VALID_TYPES = {"cut", "speed", "overlay", "keep", "edit", "timelapse"}


# What the director may emit.  ``speed`` is off the menu: three rounds of
# prompt tightening bounded the mild 1.3-2.0 band and it kept coming back
# (57.4% -> 1.4% -> 49.0% -> 29.9% of the finished video across four runs of the
# same footage), because a middle option is always cheaper to choose than a
# timelapse.  It stays in VALID_TYPES because it is still the marker a
# timelapse desugars into (guided_edit.timelapse.expand_timelapse_ops, which
# runs after parsing) and because a human may still write one into the
# hand-editable _director.json.
MENU_TYPES = VALID_TYPES - {"speed"}


# What counts as a real timelapse; the parse-time floor for
# a "timelapse" op (see parse_op) below which it is a mild speed-up instead.
TIMELAPSE_MIN_FACTOR = 4.0


@dataclass
class DirectorOp:
    type: str
    lines: tuple[int, int]  # 1-based inclusive (start, end)
    note: str = ""
    factor: float | None = None
    text: str | None = None
    duration: float | None = None  # overlay: on-screen seconds (edited timeline)
    # True when that edge of the range is the SILENCE after the line, written
    # "n~" in _director.json.  ``lines`` stays the speech-line numbers, so
    # everything that blocks, clips, sorts or reports by line keeps working;
    # only the resolver that turns an op into times reads these.  A span
    # otherwise ends at its last line's last word, which is why "compress the
    # wait after line 53" was unexpressible.
    gap_start: bool = False
    gap_end: bool = False
    # edit: the exact text as the director reads the line, and its
    # replacement ("" deletes).  guided_edit wraps it as {{old->new}}.
    old: str | None = None
    new: str | None = None
    extra: dict = field(default_factory=dict)


def coerce_lines(value: Any, num_lines: int | None, first_line: int = 1) -> tuple[int, int] | None:
    """Validate a ``lines`` value into a 1-based inclusive (start, end) range.

    ``num_lines`` of ``None`` drops the upper bound, for reading a *different*
    video's ``_director.json`` (whose transcript is not at hand).

    ``first_line`` is the segment's first line in the SOURCE's numbering.  Line
    numbers stay absolute across a split source — ``guided_edit`` and
    ``intervals`` apply ops to the whole source file — so a segment starting at
    line 31 accepts ops in ``[31, num_lines]`` and nothing outside it: an op
    crossing a reorder boundary is unrepresentable rather than something to
    detect and clip.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return None
    if isinstance(value, int):
        start = end = value
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
        if isinstance(start, bool) or isinstance(end, bool):
            return None
        if not isinstance(start, int) or not isinstance(end, int):
            return None
    else:
        return None
    if not (max(1, first_line) <= start <= end):
        return None
    if num_lines is not None and end > num_lines:
        return None
    return (start, end)


#: ``"53~"`` — the silence after source line 53.  Anchored at both ends so a
#: bare ``"~53"`` or a trailing-space variant is a malformed op, not a silent
#: reinterpretation of which line it names.
_SILENCE_REF_RE = re.compile(r"^(\d+)~$")


def _coerce_endpoint(value: Any) -> tuple[int, bool] | None:
    """One range endpoint: ``12`` -> ``(12, False)``; ``"12~"`` -> ``(12, True)``."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return None
    if isinstance(value, int):
        return (value, False)
    if isinstance(value, str):
        m = _SILENCE_REF_RE.match(value)
        if m:
            return (int(m.group(1)), True)
    return None


def _coerce_line_range(
    value: Any, num_lines: int | None, first_line: int = 1
) -> tuple[tuple[int, int], bool, bool] | None:
    """A director op's ``lines``: the same range as :func:`coerce_lines`, plus
    which edges name the silence after their line.

    Separate from :func:`coerce_lines` because ``summary`` shares that one for
    part ranges, where a silence reference has no meaning.  Every range check
    is the same: a silence edge is an edge of the SAME line, so it relaxes
    nothing — except ``"0~"``, the leading silence, which has no line of its
    own to hang from and is accepted as ``0`` when *first_line* is the
    source's start.
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        first, last = value
    else:
        first = last = value
    start = _coerce_endpoint(first)
    end = _coerce_endpoint(last)
    if start is None or end is None:
        return None
    # "0~" is the leading silence, before line 1: only a range that starts at
    # the source's true start may name it, and only as a silence edge.
    lowest = 0 if start[1] and first_line <= 1 else max(1, first_line)
    if not (lowest <= start[0] <= end[0]):
        return None
    if end[0] == 0 and not end[1]:
        return None
    if num_lines is not None and end[0] > num_lines:
        return None
    return ((start[0], end[0]), start[1], end[1])


def parse_op(
    raw: Any,
    num_lines: int | None,
    drops: list[str] | None = None,
    first_line: int = 1,
) -> DirectorOp | None:
    """One op as the director wrote it, validated; ``None`` (with the reason
    appended to *drops*) when it is malformed.

    Used by the agent skill's ``write`` tool and every reader of a
    ``_director.json`` (:func:`ops_from_dict`), so the
    timelapse factor floor, the overlay text/duration rules and the range
    checks are one implementation.
    """

    def _drop(msg: str) -> None:
        logger.warning("Director op dropped: %s", msg)
        if drops is not None:
            drops.append(msg)

    if not isinstance(raw, dict):
        return None
    op_type = raw.get("type")
    if op_type not in VALID_TYPES:
        _drop(f"unknown type {op_type!r}")
        return None
    coerced = _coerce_line_range(raw.get("lines"), num_lines, first_line)
    if coerced is None:
        _drop(f"bad lines {raw.get('lines')!r}")
        return None
    lines, gap_start, gap_end = coerced

    # The max_keep_lines cap itself is enforced as a post-pass over the whole
    # op list (see _apply_keep_cap), not here.

    note = str(raw.get("note", "") or "")

    factor: float | None = None
    if op_type in ("speed", "timelapse"):
        raw_factor = raw.get("factor")
        if not isinstance(raw_factor, (int, float)) or isinstance(raw_factor, bool):
            _drop(f"{op_type} op missing factor")
            return None
        factor = float(raw_factor)
        if factor <= 0:
            _drop(f"{op_type} factor {factor!r} <= 0")
            return None
        # The floor is part of what "timelapse" means, not a policy cap: the op
        # carries an uncapped keep, and below this a mild speed-up would smuggle
        # one over talking. A slow span is a plain `speed` op instead.
        if op_type == "timelapse" and factor < TIMELAPSE_MIN_FACTOR:
            _drop(
                f"timelapse factor {factor!r} < {TIMELAPSE_MIN_FACTOR}; "
                "below that it is a mild speed-up, not a timelapse"
            )
            return None

    text: str | None = None
    duration: float | None = None
    if op_type == "timelapse":
        # The caption is optional — a timelapse with no text is still a valid
        # continuity fix.  Its on-screen duration is derived downstream, never
        # stated, so any supplied `duration` is ignored.
        raw_text = raw.get("text")
        if isinstance(raw_text, str):
            normalised = raw_text.replace("\r\n", "\n").replace("\r", "\n")
            text = normalised if normalised.strip() else None

    if op_type == "overlay":
        raw_text = raw.get("text")
        if not isinstance(raw_text, str) or raw_text == "":
            _drop("overlay op empty/missing text")
            return None
        # A multi-line caption is fine — Blender renders the break — but only
        # "\n" is: a lone CR would survive into the single-line _edits.txt
        # marker and split it on any reader that honours it.
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            _drop("overlay op text is blank")
            return None
        # The duration is what makes the op applicable at all: an overlay's
        # on-screen time is stated, never derived from where a tag landed.
        raw_duration = raw.get("duration")
        if not isinstance(raw_duration, (int, float)) or isinstance(raw_duration, bool):
            _drop("overlay op missing duration")
            return None
        duration = float(raw_duration)
        if duration <= 0:
            _drop(f"overlay duration {duration!r} <= 0")
            return None

    old: str | None = None
    new: str | None = None
    if op_type == "edit":
        # One exact patch on one line: a {{old->new}} cannot span lines, and
        # one _edits.txt line is one WhisperX segment.
        if lines[0] != lines[1] or gap_start or gap_end:
            _drop(f"edit op names lines {list(lines)}; an edit op names exactly one line")
            return None
        raw_old, raw_new = raw.get("old"), raw.get("new")
        if not isinstance(raw_old, str) or raw_old == "":
            _drop("edit op needs 'old': the exact text to change, as view shows the line")
            return None
        if not isinstance(raw_new, str):
            _drop("edit op needs 'new': the replacement text (\"\" deletes 'old')")
            return None
        for name, value in (("old", raw_old), ("new", raw_new)):
            reason = forbidden_problem(name, value)
            if reason:
                _drop(reason)
                return None
        old, new = raw_old, raw_new

    return DirectorOp(
        type=op_type,
        lines=lines,
        note=note,
        factor=factor,
        text=text,
        duration=duration,
        gap_start=gap_start,
        gap_end=gap_end,
        old=old,
        new=new,
    )


def keep_cap_problem(op: DirectorOp, max_keep_lines: int) -> str | None:
    """Why *op* breaks the ``keep`` width cap, or ``None``.

    The agent skill's ``write`` tool refuses an edit with one.  ``0`` = no limit.
    """
    if max_keep_lines <= 0 or op.type != "keep":
        return None
    span = op.lines[1] - op.lines[0] + 1
    if span <= max_keep_lines:
        return None
    return (
        f"keep op spans {span} lines {list(op.lines)} > "
        f"max_keep_lines={max_keep_lines}; a keep may span a "
        "continuous on-screen event, not a talking span"
    )


def off_menu_problem(op: DirectorOp) -> str | None:
    """Why *op* is not an operation the director may emit, or ``None``.

    Currently that is ``speed`` only (see :data:`MENU_TYPES`).  Shared by the
    agent skill's tools.
    """
    if op.type in MENU_TYPES:
        return None
    return (
        f"{op.type!r} op {list(op.lines)} is not an operation the "
        'director may emit; a stretch worth going fast over is a "timelapse", '
        'anything else plays at 1x or is a "cut"'
    )


def ops_from_dict(data: Any, num_lines: int | None) -> list[DirectorOp]:
    """Load validated ops from a parsed ``_director.json`` dict.

    Same validation as :func:`parse_director_response`; invalid/out-of-range
    ops are skipped.  ``num_lines=None`` skips the range check, for reading
    another video's file where the transcript length is unknown.  The ``max_keep_lines`` cap is deliberately NOT applied
    here: ``_director.json`` is a hand-editable intermediate, so a human who
    writes a wide ``keep`` into it means it.  The cap guards the LLM's output
    only, at generation time.
    """
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        return []
    ops: list[DirectorOp] = []
    for raw in data["ops"]:
        op = parse_op(raw, num_lines)
        if op is not None:
            ops.append(op)
    return ops


def collect_overlay_texts(ops: Sequence[DirectorOp]) -> list[str]:
    """The captions the director placed, in order, deduped.

    These are the moments the edit itself calls out — a mishap, a result, a
    conclusion — which is exactly the register a title or a thumbnail hook
    wants, so they are handed to the LLM verbatim.
    """
    out: list[str] = []
    for op in ops:
        if op.type not in ("overlay", "timelapse"):
            continue
        text = (op.text or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def ops_to_dict(ops: list[DirectorOp]) -> dict[str, Any]:
    """Serialise ops to the ``{stem}_director.json`` shape."""
    out: list[dict[str, Any]] = []
    for op in ops:
        entry: dict[str, Any] = {
            "type": op.type,
            "lines": [
                f"{op.lines[0]}~" if op.gap_start else op.lines[0],
                f"{op.lines[1]}~" if op.gap_end else op.lines[1],
            ],
        }
        if op.factor is not None:
            entry["factor"] = op.factor
        if op.text is not None:
            entry["text"] = op.text
        if op.duration is not None:
            entry["duration"] = op.duration
        if op.old is not None:
            entry["old"] = op.old
            entry["new"] = op.new or ""
        if op.note:
            entry["note"] = op.note
        out.append(entry)
    return {"ops": out}


def max_keep_lines(director_cfg: dict[str, Any]) -> int:
    """Read ``director.max_keep_lines`` defensively (``0``/invalid = no limit)."""
    raw = director_cfg.get("max_keep_lines", 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def keep_limit_note(max_keep_lines: int) -> str:
    """The keep cap as the director is told it (``view`` prints it)."""
    return (
        f'A "keep" op may span at most {max_keep_lines} line(s), and keeps that touch '
        "or overlap count as one keep against that limit; write refuses either. Cover "
        "an on-screen event with ONE keep over its whole range, short noise/utterance "
        "lines included; for one wider than that, ask the user to raise "
        "director.max_keep_lines (or consider a timelapse for manual work). "
        'A "timelapse" op needs no keep of its own; it protects its whole range by '
        "itself."
    )
