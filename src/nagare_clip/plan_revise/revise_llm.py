"""plan_revise: revise an existing plan by naming only what changes.

The model receives the parts document with the current directions rendered
under the part each belongs to, every one tagged with a short id, plus the
conversation with the human editor.  It answers with *operations*::

    {"delete": ["k7f2"],
     "add":    [{"index": 21, "lines": [60, 83], "direction": "feature — …"}],
     "update": [{"id": "m3q8", "direction": "shorten heavily — …"}],
     "message": "パート21を3つに分割しました…"}

A direction no operation names is carried through **by the code**, not by the
model's diligence: restating a whole plan to change one line grows with the
project and invites the model to economise, and an omission is then
indistinguishable from a deletion.  ``add`` carries no insertion position —
a direction is located by its part ``index`` and its ``lines``, which fully
determine the order.

Graceful by design: a malformed operation is dropped and logged; only invalid
JSON (or a response with no operations *and* no message) is a hard failure that
retries.  All attempts failing leaves the plan untouched and says so, so the
next run tries again instead of silently answering the human's turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from nagare_clip.director.director_llm import _FENCE_RE
from nagare_clip.llm_client import with_trace_meta
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
from nagare_clip.plan.dialogue import DialogueTurn, render_history
from nagare_clip.plan.plan_llm import (
    CallLLM,
    PartDirection,
    coerce_lines,
    format_parts_for_plan,
)
from nagare_clip.plan_revise.ids import assign_ids, resolve
from nagare_clip.summary.summarize import PartSummary, ProjectSummary
from nagare_clip.text_filter.llm_filter import _call_llm

logger = logging.getLogger(__name__)

CONVERSATION_HEADER = "Conversation with the human editor (oldest first):"
_OP_KEYS = ("delete", "add", "update", "message")


@dataclass
class ReviseOps:
    """What one revision response asks for (ids are not resolved yet)."""

    delete: list[str] = field(default_factory=list)
    add: list[PartDirection] = field(default_factory=list)
    update: list[tuple[str, str]] = field(default_factory=list)
    message: str = ""

    @property
    def count(self) -> int:
        return len(self.delete) + len(self.add) + len(self.update)

    @property
    def applied(self) -> str:
        return f"{len(self.add)} added, {len(self.delete)} deleted, {len(self.update)} updated"


@dataclass
class Revision:
    """The merged plan, plus what to say back and whether it may be written."""

    directions: list[PartDirection]
    message: str = ""
    applied: str = ""
    ok: bool = False


def build_user_content(
    project_summary: ProjectSummary,
    directions: list[PartDirection],
    ids: list[str],
    turns: list[DialogueTurn],
) -> str:
    content = format_parts_for_plan(project_summary, directions, ids)
    if turns:
        content += "\n\n" + CONVERSATION_HEADER + "\n\n" + render_history(turns)
    return content


def _entries(data: dict[str, Any], key: str) -> list[Any]:
    """The list under *key* — a non-list (or absent) key is simply no operations."""
    value = data.get(key)
    return value if isinstance(value, list) else []


def try_parse_revision(
    response: str, parts: list[PartSummary], drops: list[str] | None = None
) -> ReviseOps | None:
    """Parse one revision response, or ``None`` on a hard parse failure."""

    def _drop(msg: str) -> None:
        logger.warning("plan_revise: %s", msg)
        if drops is not None:
            drops.append(msg)

    text = response.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("plan_revise: response is not valid JSON; ignoring")
        return None
    if not isinstance(data, dict) or not any(k in data for k in _OP_KEYS):
        logger.warning("plan_revise: response has no operations and no message; ignoring")
        return None

    ops = ReviseOps()
    message = data.get("message")
    ops.message = message if isinstance(message, str) else ""

    for raw in _entries(data, "delete"):
        if not isinstance(raw, str) or not raw.strip():
            _drop(f"delete dropped, bad id {raw!r}")
            continue
        ops.delete.append(raw.strip())

    for raw in _entries(data, "add"):
        parsed = _parse_add(raw, parts, _drop)
        if parsed is not None:
            ops.add.append(parsed)

    for raw in _entries(data, "update"):
        if not isinstance(raw, dict):
            _drop(f"update dropped, not an object: {raw!r}")
            continue
        ident, direction = raw.get("id"), raw.get("direction")
        if not isinstance(ident, str) or not ident.strip():
            _drop(f"update dropped, bad id {ident!r}")
            continue
        if not isinstance(direction, str) or not direction:
            _drop(f"update dropped, empty/missing text for id {ident!r}")
            continue
        ops.update.append((ident.strip(), direction))

    return ops


def _parse_add(raw: Any, parts: list[PartSummary], drop) -> PartDirection | None:
    """One ``add`` entry, addressed exactly as a plan direction is."""
    if not isinstance(raw, dict):
        drop(f"add dropped, not an object: {raw!r}")
        return None
    idx = raw.get("index")
    if isinstance(idx, bool) or not isinstance(idx, int) or not (1 <= idx <= len(parts)):
        drop(f"add dropped, bad/out-of-range index {idx!r}")
        return None
    part = parts[idx - 1]
    direction = raw.get("direction")
    if not isinstance(direction, str) or not direction:
        drop(f"add dropped, empty/missing text for part {idx}")
        return None
    lines = part.lines
    if raw.get("lines") is not None:
        narrowed = coerce_lines(raw.get("lines"), part)
        if narrowed is None:
            drop(
                f"add dropped, lines {raw.get('lines')!r} outside "
                f"part {idx} range {part.lines[0]}-{part.lines[1]}"
            )
            return None
        lines = narrowed
    return PartDirection(stem=part.stem, lines=lines, direction=direction)


def _order(parts: list[PartSummary]):
    """Part-then-line order — the same order ``plan`` writes."""
    first: dict[str, int] = {}
    for i, p in enumerate(parts):
        first.setdefault(p.stem, i)
    return lambda d: (first.get(d.stem, len(parts)), d.lines)


def apply_revision(
    directions: list[PartDirection],
    ops: ReviseOps,
    parts: list[PartSummary],
    drops: list[str] | None = None,
) -> list[PartDirection]:
    """Merge *ops* into *directions*; everything unnamed survives untouched."""

    def _drop(msg: str) -> None:
        logger.warning("plan_revise: %s", msg)
        if drops is not None:
            drops.append(msg)

    work = list(directions)
    for ident, text in ops.update:
        hits = resolve(work, ident)
        if not hits:
            _drop(f"update dropped, unknown id {ident!r}")
            continue
        for i in hits:
            work[i] = PartDirection(work[i].stem, work[i].lines, text)

    removed: set[int] = set()
    for ident in ops.delete:
        hits = resolve(work, ident)
        if not hits:
            _drop(f"delete dropped, unknown id {ident!r}")
            continue
        removed.update(hits)

    # Keyed by the footage a direction covers, so an added range replaces the
    # direction that already covered exactly it (the same rule plan parses by).
    merged: dict[tuple[str, tuple[int, int]], PartDirection] = {
        (d.stem, d.lines): d for i, d in enumerate(work) if i not in removed
    }
    for added in ops.add:
        merged[(added.stem, added.lines)] = added
    return sorted(merged.values(), key=_order(parts))


def generate_revision(
    project_summary: ProjectSummary,
    directions: list[PartDirection],
    turns: list[DialogueTurn],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    unit: str = "plan_revise",
) -> Revision:
    """One call: the conversation applied to *directions* as operations."""
    parts = project_summary.parts
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {
            "role": "user",
            "content": build_user_content(
                project_summary, directions, assign_ids(directions), turns
            ),
        },
    ]
    recorder.begin(unit)
    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "plan_revise: LLM call failed (attempt %d/%d)", attempt + 1, attempts, exc_info=True
            )
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=messages,
                error=str(e),
                outcome=LLM_ERROR,
                reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        drops: list[str] = []
        ops = try_parse_revision(response, parts, drops)
        if ops is None:
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=messages,
                response=response,
                outcome=UNPARSEABLE,
                reason="invalid JSON / no operations and no message",
                cfg=attempt_cfg,
            )
            logger.warning(
                "plan_revise: response unparseable (attempt %d/%d)", attempt + 1, attempts
            )
            continue
        revised = apply_revision(directions, ops, parts, drops)
        if drops:
            outcome, reason = DROPPED_ITEMS, f"{len(drops)} dropped: " + "; ".join(drops)
        elif ops.count:
            outcome, reason = OK, ""
        else:
            outcome, reason = OK_EMPTY, ""
        recorder.attempt(
            unit=unit,
            attempt=attempt,
            total=attempts,
            messages=messages,
            response=response,
            outcome=outcome,
            reason=reason,
            cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=outcome, reason=reason)
        logger.info("plan_revise: %s", ops.applied)
        return Revision(directions=revised, message=ops.message, applied=ops.applied, ok=True)
    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("plan_revise: all %d attempt(s) failed; the plan is left as it was", attempts)
    return Revision(directions=list(directions), message="", applied="", ok=False)
