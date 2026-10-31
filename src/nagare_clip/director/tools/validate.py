"""Validate an agent's edit, in display numbering, all problems at once.

An edit is one JSON object::

    {"plan": "<prose>", "order": [[first, last], ...], "ops": [{...}, ...]}

The edit is the complete state, like the files it writes: ``ops`` is required
and replaces every op, and an absent ``order`` means shooting order — ``write``
always writes ``order.json``, the identity order when ``order`` is absent, so
writing the same edit twice gives the same files.  ``plan`` is optional (the
plan on disk stays), but required while ``plan.md`` holds none.

Every check is the LLM director's own — :func:`~..director_llm.parse_op`
(types, factor floor, overlay text/duration, range bounds),
:func:`~..director_llm.off_menu_problem` (no ``speed``),
:func:`~..director_llm.keep_cap_problem`, :func:`~..loop.join_problem`,
:func:`~..loop.silence_ref_problem`, :func:`~..loop.order_problems` and
:func:`~..loop.break_problem` — plus two that only the tools enforce: a ``cut``
range may not overlap any other op's range (the LLM prompt asks for it and
``guided_edit`` clips instead), an ``edit`` op may not sit on a silence line
(``guided_edit`` could not apply it), and keeps that touch or overlap count as
ONE keep against ``director.max_keep_lines`` — chaining them would restore the
very span the cap exists to keep dropped.  Unlike the LLM path, nothing is dropped
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
    *order* is ``None`` for shooting order (no ``order`` in the edit); *plan*
    is ``None`` when the edit left the plan on disk alone.  :func:`to_source` converts the ops.
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
    has_plan: bool,
    max_keep_lines: int = 0,
) -> tuple[Edit, list[Problem]]:
    """Check *data* against *view*; ``(edit, [])`` or ``(partial edit, problems)``.

    The partial edit holds the ops that passed their own checks, so a preview
    can still show them; only an edit with no problems may be written.
    The timelapse/break checks use the edit's own order (shooting order when it
    has none), never the order on disk.  *has_plan* says whether ``plan.md``
    already holds a plan.
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
    in_force = order or []

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

    problems.extend(_keep_chain_problems(view, accepted, max_keep_lines))
    problems.sort(key=lambda p: p.op)  # stable: edit/plan/order first, then by op
    return Edit(ops=[(span, op) for _i, span, op in accepted], order=order, plan=plan), problems


def _keep_chain_problems(
    view: DisplayView, accepted: list[tuple[int, Range, DirectorOp]], max_keep_lines: int
) -> list[Problem]:
    """Keeps that touch or overlap, together wider than the cap.

    A single over-wide keep is :func:`keep_cap_problem`'s; this catches the
    same span split into pieces that each fit.  A source boundary ends a chain:
    the two sides are different recordings, not one event.  Reported on the
    chain's first op.
    """
    if max_keep_lines <= 0:
        return []
    keeps = sorted((span, i) for i, span, op in accepted if op.type == "keep")
    chains: list[list[tuple[Range, int]]] = []
    for span, i in keeps:
        if (
            chains
            and span[0] <= max(s[1] for s, _ in chains[-1]) + 1
            and view.line(span[0]).segment == view.line(chains[-1][0][0][0]).segment
        ):
            chains[-1].append((span, i))
        else:
            chains.append([(span, i)])
    out: list[Problem] = []
    for chain in chains:
        if len(chain) < 2:
            continue
        first = chain[0][0][0]
        last = max(s[1] for s, _ in chain)
        width = last - first + 1
        if width <= max_keep_lines:
            continue
        i = min(n for _, n in chain)
        ops = ", ".join(f"#{n}" for _, n in sorted(chain, key=lambda c: c[1]))
        out.append(
            Problem(
                f"op #{i} (keep chain [{first}, {last}])",
                f"keeps {ops} touch and together span {width} lines > "
                f"max_keep_lines={max_keep_lines}; consecutive keeps count as one. "
                "To keep the silences of a long on-screen event, put one keep on each "
                "silence line ([n, n]) — the speech lines between them play anyway, so "
                "those keeps do not touch. If the event is manual work, a timelapse may "
                "fit better. To keep the short pauses too, ask the user to raise "
                "director.max_keep_lines and use ONE keep",
                i,
            )
        )
    return out


def skill_keep_limit_note(max_keep_lines: int) -> str:
    """The keep cap as the agent skill states it (the LLM director keeps its own
    :func:`~..director_llm.keep_limit_note`, unchanged)."""
    return (
        f'A "keep" op may span at most {max_keep_lines} line(s), and keeps that touch '
        "or overlap count as one keep against that limit; write refuses either. To "
        "keep the silences of a long on-screen event, put one keep on each silence "
        "line ([n, n]): the speech between them plays anyway, so they do not touch. "
        "Wider than that only with the user's OK to raise director.max_keep_lines. "
        'A "timelapse" op needs no keep of its own; it protects its whole range by '
        "itself."
    )


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
