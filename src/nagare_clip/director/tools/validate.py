"""Validate an agent's edit, in display numbering, all problems at once.

An edit is one JSON object::

    {"plan": "<prose>", "order": [[first, last], ...], "ops": [{...}, ...]}

``ops`` is required and replaces every op the director files hold.  ``plan``
and ``order`` are optional: absent, the ones already on disk stay in force
(and the timelapse checks run against that order).

Every check is the LLM director's own — :func:`~..director_llm.parse_op`
(types, factor floor, overlay text/duration, range bounds),
:func:`~..director_llm.off_menu_problem` (no ``speed``),
:func:`~..director_llm.keep_cap_problem`, :func:`~..loop.join_problem`,
:func:`~..loop.silence_ref_problem`, :func:`~..loop.order_problems` and
:func:`~..loop.break_problem` — plus two that only the tools enforce: a ``cut``
range may not overlap any other op's range (the LLM prompt asks for it and
``guided_edit`` clips instead), and an ``edit`` op may not sit on a silence line
(``guided_edit`` could not apply it).  Unlike the LLM path, nothing is dropped
or split silently: a problem refuses the whole edit, with its reason.

Pure: no I/O, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from nagare_clip.director.director_llm import (
    DirectorOp,
    keep_cap_problem,
    off_menu_problem,
    parse_op,
)
from nagare_clip.director.display import DisplayView
from nagare_clip.director.loop import (
    Range,
    break_problem,
    join_problem,
    order_breaks,
    order_problems,
    silence_ref_problem,
)

KNOWN_KEYS = frozenset({"plan", "order", "ops"})


@dataclass(frozen=True)
class Problem:
    """One thing wrong with an edit: where (``op #i``/``order``/...) and why."""

    where: str
    reason: str
    #: The op's 1-based position in ``ops``; ``0`` for the edit, plan and order.
    op: int = 0

    def __str__(self) -> str:
        return f"{self.where}: {self.reason}"


@dataclass(frozen=True)
class Edit:
    """A validated edit.

    *ops* are ``(display range, op in DISPLAY numbering)`` in the order given;
    *order* is ``None`` when the edit left the order in force alone, *plan*
    likewise.  :func:`to_source` converts the ops.
    """

    ops: list[tuple[Range, DirectorOp]] = field(default_factory=list)
    order: list[Range] | None = None
    plan: str | None = None


def _label(i: int, raw: Any) -> str:
    if isinstance(raw, dict):
        return f"op #{i} ({raw.get('type')!s} {raw.get('lines')!r})"
    return f"op #{i}"


def _overlaps(a: Range, b: Range) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def validate_edit(
    view: DisplayView,
    data: Any,
    *,
    current_order: list[Range],
    has_plan: bool,
    max_keep_lines: int = 0,
) -> tuple[Edit, list[Problem]]:
    """Check *data* against *view*; ``(edit, [])`` or ``(partial edit, problems)``.

    The partial edit holds the ops that passed their own checks, so a preview
    can still show them; only an edit with no problems may be written.
    *current_order* is the order on disk (display ranges, ``[]`` = shooting
    order); *has_plan* says whether ``plan.md`` already holds a plan.
    """
    total = len(view.lines)
    problems: list[Problem] = []
    if not isinstance(data, dict):
        return Edit(), [Problem("edit", 'must be a JSON object {"plan"?, "order"?, "ops"}')]
    for key in sorted(set(data) - KNOWN_KEYS):
        problems.append(Problem("edit", f'unknown key "{key}" (allowed: plan, order, ops)'))

    plan: str | None = None
    if "plan" in data:
        raw_plan = data["plan"]
        if not isinstance(raw_plan, str) or not raw_plan.strip():
            problems.append(Problem("plan", "must be the plan as non-empty text"))
        else:
            plan = raw_plan.strip()
    elif not has_plan:
        problems.append(Problem("plan", 'there is no plan yet (no director/plan.md): add "plan"'))

    order: list[Range] | None = None
    if "order" in data:
        order, why = order_problems(view, data["order"], [])
        problems.extend(Problem("order", reason) for reason in why)
    in_force = order if order is not None else current_order

    raw_ops = data.get("ops")
    if not isinstance(raw_ops, list):
        problems.append(
            Problem("ops", "required: a list of ops ([] for none); it replaces every op")
        )
        raw_ops = []

    accepted: list[tuple[int, Range, DirectorOp]] = []
    for i, raw in enumerate(raw_ops, start=1):
        where = _label(i, raw)
        if not isinstance(raw, dict):
            problems.append(Problem(where, "not a JSON object", i))
            continue
        drops: list[str] = []
        op = parse_op(raw, total, drops)
        if op is None:
            reason = "; ".join(drops) or "malformed"
            if reason.startswith("bad lines"):
                reason += f" — give [first, last] display line numbers within 1-{total}"
            problems.append(Problem(where, reason, i))
            continue
        span: Range = op.lines
        reasons = [
            silence_ref_problem(op),
            off_menu_problem(op),
            keep_cap_problem(op, max_keep_lines),
            join_problem(view, op.type, *span, splittable=frozenset()),
        ]
        if op.type == "edit":
            silent = [n for n in range(span[0], span[1] + 1) if view.lines[n - 1].is_silence]
            if silent:
                reasons.append(
                    f"line {silent[0]} is a silence line; an edit op changes words, "
                    "so it must name speech lines only"
                )
        found = [r for r in reasons if r]
        problems.extend(Problem(where, r, i) for r in found)
        if not found:
            accepted.append((i, span, op))

    breaks = order_breaks(in_force, total)
    for i, span, op in accepted:
        reason = break_problem(op.type, span, breaks)
        if reason:
            problems.append(Problem(f"op #{i} (timelapse {list(span)})", reason, i))

    for i, span, op in accepted:
        if op.type != "cut":
            continue
        for j, other_span, other in accepted:
            if j == i or not _overlaps(span, other_span):
                continue
            if other.type == "cut" and j < i:
                continue  # reported once, on the earlier cut
            problems.append(
                Problem(
                    f"op #{i} (cut {list(span)})",
                    f"overlaps op #{j} ({other.type} {list(other_span)}); a cut range must "
                    "not overlap any other op's range — shorten the cut so the other op's "
                    "lines are outside it",
                    i,
                )
            )

    problems.sort(key=lambda p: p.op)  # stable: edit/plan/order first, then by op
    return Edit(ops=[(span, op) for _i, span, op in accepted], order=order, plan=plan), problems


def to_source(view: DisplayView, edit: Edit) -> dict[int, list[DirectorOp]]:
    """*edit*'s ops in source coordinates, keyed by segment index.

    Every op here has passed :func:`validate_edit`, so each range lies inside one
    segment and maps (a silence line at an edge becomes that edge's ``"n~"``).
    """
    out: dict[int, list[DirectorOp]] = {}
    for span, op in edit.ops:
        mapped = view.to_source(*span)
        line = view.line(span[0])
        if mapped is None or line is None:  # pragma: no cover - validated
            raise ValueError(f"{op.type} {list(span)} does not map to one segment")
        _stem, lines, gap_start, gap_end = mapped
        out.setdefault(line.segment, []).append(
            replace(op, lines=lines, gap_start=gap_start, gap_end=gap_end)
        )
    for ops in out.values():
        ops.sort(key=lambda op: (op.lines[0], op.gap_start, op.lines[1], op.gap_end))
    return out


def to_display(view: DisplayView, index: int, op: DirectorOp) -> dict[str, Any] | None:
    """One source-coordinate op as an edit-file op in display numbering."""
    a = view.from_source(index, op.lines[0], silence=op.gap_start)
    b = view.from_source(index, op.lines[1], silence=op.gap_end)
    if a is None or b is None:
        return None
    out: dict[str, Any] = {"type": op.type, "lines": [a, b]}
    if op.factor is not None:
        out["factor"] = op.factor
    if op.text is not None:
        out["text"] = op.text
    if op.duration is not None:
        out["duration"] = op.duration
    out["note"] = op.note
    return out
