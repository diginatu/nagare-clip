"""plan_revise stage: apply the conversation to the plan, as operations.

The stage owns ``plan_dialogue/history.md``.  It fires **only when a human turn
is unanswered** — no conversation, no LLM call — which makes the common case
free instead of costing one call to say "no changes requested".  ``plan`` is
left as a pure function of the summaries.

It writes ``plan_revise/plan.json``, never into ``plan/``: one directory, one
owning stage.  ``director`` prefers the revised file when it exists, so
``diff plan/plan.json plan_revise/plan.json`` is exactly the human's influence
on the edit.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.brief import apply_brief
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.plan.dialogue import (
    PLAN,
    append_turn,
    ensure_history,
    has_unanswered_human,
    read_active_history,
)
from nagare_clip.plan.plan_llm import (
    PartDirection,
    order_from_dict,
    plan_from_dict,
    plan_to_dict,
)
from nagare_clip.plan_revise.revise_llm import generate_revision
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict


def _read_json(path: Path, what: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logging.warning("plan_revise: could not read the %s %s", what, path)
        return None


def _discard(output: Path) -> None:
    if not output.is_file():
        return
    try:
        output.unlink()
        logging.info("plan_revise: disabled, removed the stale %s", output)
    except OSError as e:
        logging.warning("plan_revise: could not remove %s: %s", output, e)


def run_plan_revise(
    summary_json: Path,
    plan_json: Path,
    output: Path,
    cfg: dict,
    *,
    history: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
    line_counts: dict[str, int] | None = None,
) -> None:
    revise_cfg = cfg["plan_revise"]

    if not revise_cfg.get("enabled", False):
        _discard(output)
        return

    if history is not None:
        # Refresh the header whenever the conversation is read at all, not only
        # when a turn is appended.  The quiet run is exactly when a human opens
        # this file to type, so it is the run that most needs the instructions
        # on screen to be the current ones.  Nothing else is written on this
        # path: no divider, no reply slot, no turn.
        ensure_history(history)

    turns = read_active_history(history)
    if not has_unanswered_human(turns):
        logging.info("plan_revise: nothing unanswered in the conversation; no LLM call")
        return

    data = _read_json(summary_json, "summary")
    if data is None:
        return
    project_summary: ProjectSummary = summary_from_dict(data)

    plan_data = _read_json(plan_json, "plan") or {}
    directions: list[PartDirection] = plan_from_dict(plan_data)
    # The order is restated whole or inherited; either way plan_revise/plan.json
    # carries the effective one, since director reads exactly one plan file.
    current_order = order_from_dict(plan_data)
    logging.info(
        "plan_revise: revising %d direction(s) against %d conversation turn(s)",
        len(directions),
        len(turns),
    )
    revision = generate_revision(
        project_summary,
        directions,
        turns,
        apply_brief(revise_cfg, cfg),
        recorder=recorder,
        order=current_order,
        line_counts=line_counts,
    )
    if not revision.ok:
        # Nothing written and nothing answered: the next run tries again.
        logging.warning("plan_revise: no revision produced; %s left as it was", plan_json)
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(plan_to_dict(revision.directions, revision.order), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    logging.info("plan_revise: wrote %s (%d direction(s))", output, len(revision.directions))

    if history is not None:
        # Always a turn, even with nothing to say: an unanswered turn is what
        # fires the stage, so silence here would make every run pay for a call.
        append_turn(history, PLAN, revision.message or f"({revision.applied})")
