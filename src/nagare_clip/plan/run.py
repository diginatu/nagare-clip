"""plan stage: coarse cross-video rough directions per part.

When ``plan.enabled`` is false (default) it writes an empty artifact so the
downstream director is unaffected and the pipeline behaves exactly as before.

When it is enabled the stage is a *conversation*: it reads the plan it wrote
last time plus ``plan_dialogue/history.md``, updates only what the conversation
calls for, and appends its own reply to the history.  Re-running just this stage
(``--from-stage plan --to-stage plan``, one LLM call) is therefore how a human
corrects the plan, instead of hand-editing ``plan.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.brief import apply_brief
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.plan.dialogue import PLAN, append_turn, ensure_history, read_history
from nagare_clip.plan.plan_llm import (
    PartDirection,
    generate_plan,
    plan_from_dict,
    plan_to_dict,
)
from nagare_clip.summary.summarize import summary_from_dict


def _previous_plan(output: Path) -> list[PartDirection]:
    """The plan.json this run is about to overwrite (empty on the first run)."""
    if not output.is_file():
        return []
    try:
        return plan_from_dict(json.loads(output.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        logging.warning("plan: could not read previous plan %s", output)
        return []


def run_plan(
    summary_json: Path,
    output: Path,
    cfg: dict,
    *,
    history: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    plan_cfg = cfg["plan"]

    if not plan_cfg.get("enabled", False):
        logging.info("plan: disabled, writing empty plan")
        directions = []
    else:
        project_summary = summary_from_dict(json.loads(summary_json.read_text(encoding="utf-8")))
        turns = read_history(history)
        previous = _previous_plan(output)
        logging.info(
            "plan: directing %d part(s) with LLM (%d previous direction(s), %d turn(s))",
            len(project_summary.parts),
            len(previous),
            len(turns),
        )
        result = generate_plan(
            project_summary,
            apply_brief(plan_cfg, cfg),
            recorder=recorder,
            previous=previous,
            history=turns,
        )
        directions = result.directions
        logging.info("plan: %d direction(s)", len(directions))
        if history is not None:
            # Created even with nothing to say, so a human can find where to reply.
            ensure_history(history)
            if result.message:
                append_turn(history, PLAN, result.message)
                logging.info("plan: appended its turn to %s", history)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(plan_to_dict(directions), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("plan: wrote %s", output)
