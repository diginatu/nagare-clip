"""Where the director's ops contradict the plan's directions.

Deterministic and free: it runs after the ``director`` stage with no LLM call
and only *reports*.  The director is deliberately NOT made to obey the plan — it
is the first stage that reads the actual lines, and its override is often right.
What was missing was any record that the two stages disagreed; a human had to
diff 22 directions against 61 ops by hand to find it.

Now that the plan can be corrected in conversation (``plan_dialogue/``), a
surviving divergence is a signal worth reading rather than an artifact of a plan
that was coarse by construction.

Direction text is free-form, so the verb is read from the direction's leading
clause (before the first dash/colon) — the ``feature — why`` shape the default
prompt asks for.  A direction whose clause carries no known verb is ignored
rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.plan.plan_llm import PartDirection

# The plan vocabulary (its prompt bans the word "keep" on purpose).
FEATURE_WORDS = ("feature", "retain", "emphasise", "emphasize", "emphasis", "highlight")
REMOVE_WORDS = ("remove", "cut", "drop", "delete", "skip")
TIMELAPSE_WORDS = ("timelapse", "time-lapse", "speed")

# Ops that protect footage a direction asked to lose.
PROTECTING_TYPES = ("keep", "overlay", "timelapse")

DEFAULT_CUT_SHARE = 0.5

_CLAUSE_RE = re.compile(r"[—–\-:、。,.]")


@dataclass
class Divergence:
    stem: str
    lines: tuple[int, int]
    direction: str
    kind: str  # cut-over-feature | protected-over-remove | no-timelapse
    detail: str
    notes: list[str] = field(default_factory=list)


def _clause(direction: str) -> str:
    """The direction's leading clause, where its verb lives."""
    return _CLAUSE_RE.split(direction, maxsplit=1)[0].lower()


def _covered(lines: tuple[int, int], ops: list[DirectorOp]) -> int:
    """How many of ``lines`` at least one op covers (line granularity)."""
    hit = set()
    for op in ops:
        for n in range(max(lines[0], op.lines[0]), min(lines[1], op.lines[1]) + 1):
            hit.add(n)
    return len(hit)


def _overlapping(lines: tuple[int, int], ops: list[DirectorOp]) -> list[DirectorOp]:
    return [op for op in ops if op.lines[0] <= lines[1] and op.lines[1] >= lines[0]]


def _notes(ops: list[DirectorOp]) -> list[str]:
    """The director's own argument for the override, where it gave one."""
    return [f"{op.type} [{op.lines[0]}-{op.lines[1]}]: {op.note}".rstrip(": ") for op in ops]


def find_divergences(
    directions: list[PartDirection],
    ops_by_stem: dict[str, list[DirectorOp]],
    *,
    cut_share: float = DEFAULT_CUT_SHARE,
) -> list[Divergence]:
    """Compare each direction against the ops that landed in its line range."""
    out: list[Divergence] = []
    for d in directions:
        ops = _overlapping(d.lines, ops_by_stem.get(d.stem, []))
        clause = _clause(d.direction)
        width = d.lines[1] - d.lines[0] + 1

        if any(w in clause for w in FEATURE_WORDS):
            cuts = [op for op in ops if op.type == "cut"]
            share = _covered(d.lines, cuts) / width if width else 0.0
            if share >= cut_share:
                out.append(
                    Divergence(
                        stem=d.stem,
                        lines=d.lines,
                        direction=d.direction,
                        kind="cut-over-feature",
                        detail=(
                            f"cut ops cover {share:.2f} of the directed lines "
                            f"(threshold {cut_share:.2f})"
                        ),
                        notes=_notes(cuts),
                    )
                )
            continue

        if any(w in clause for w in REMOVE_WORDS):
            protecting = [op for op in ops if op.type in PROTECTING_TYPES]
            if protecting:
                kinds = ", ".join(sorted({op.type for op in protecting}))
                out.append(
                    Divergence(
                        stem=d.stem,
                        lines=d.lines,
                        direction=d.direction,
                        kind="protected-over-remove",
                        detail=f"{len(protecting)} protecting op(s) landed here: {kinds}",
                        notes=_notes(protecting),
                    )
                )
            continue

        if any(w in clause for w in TIMELAPSE_WORDS):
            if not [op for op in ops if op.type == "timelapse"]:
                out.append(
                    Divergence(
                        stem=d.stem,
                        lines=d.lines,
                        direction=d.direction,
                        kind="no-timelapse",
                        detail="no timelapse op landed in the directed lines",
                        notes=_notes(ops),
                    )
                )
    return out


def format_divergences(divergences: list[Divergence]) -> str:
    """A markdown block for the LLM report (empty string when they agree)."""
    if not divergences:
        return ""
    lines = [
        "## plan/director divergence",
        "",
        f"{len(divergences)} direction(s) the director's ops argue with. "
        "The director is not made to obey the plan — read the note, then either "
        "accept it or tell plan (scripts/plan_say.sh) and re-run the stage.",
        "",
    ]
    for d in divergences:
        lines.append(f"- **{d.stem} [{d.lines[0]}-{d.lines[1]}]** ({d.kind}) — {d.detail}")
        lines.append(f"  - plan: {d.direction}")
        for note in d.notes:
            lines.append(f"  - director: {note}")
    return "\n".join(lines) + "\n"
