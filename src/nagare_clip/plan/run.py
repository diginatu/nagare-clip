"""plan stage: coarse cross-video rough directions per part.

When ``plan.enabled`` is false (default) it writes an empty artifact so the
downstream director is unaffected and the pipeline behaves exactly as before.

The stage is a **pure function of ``summary.json``**: it reads neither its own
previous output nor the conversation.  Revising a plan against what the human
said is the ``plan_revise`` stage's job.

A run therefore invalidates what was built on the plan it replaces:

- ``plan_revise/plan.json`` is deleted — it revises directions that no longer
  exist, and ``director`` would keep preferring it;
- a divider is appended to ``plan_dialogue/history.md``, so the turns above it
  stop being applied.  They are **divided, not deleted**: ``plan`` re-runs often
  (a prompt change, an unrelated ``--from-stage plan``) while ``summary`` rarely
  does, so an instruction that cost a human twenty minutes of measuring footage
  is usually still true and should be copyable rather than reconstructable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.brief import apply_brief
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.plan.dialogue import append_divider
from nagare_clip.plan.plan_llm import generate_plan, plan_to_dict
from nagare_clip.summary.summarize import summary_from_dict


def _invalidate_revision(revised: Path | None) -> None:
    """Drop the revised plan built on the plan this run replaces."""
    if revised is None or not Path(revised).is_file():
        return
    try:
        Path(revised).unlink()
        logging.info("plan: invalidated the revised plan %s", revised)
    except OSError as e:
        logging.warning("plan: could not remove the stale %s: %s", revised, e)


def run_plan(
    summary_json: Path,
    output: Path,
    cfg: dict,
    *,
    history: Path | None = None,
    revised: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    plan_cfg = cfg["plan"]

    if not plan_cfg.get("enabled", False):
        logging.info("plan: disabled, writing empty plan")
        directions = []
    else:
        project_summary = summary_from_dict(json.loads(summary_json.read_text(encoding="utf-8")))
        logging.info("plan: directing %d part(s) with LLM", len(project_summary.parts))
        directions = generate_plan(
            project_summary,
            apply_brief(plan_cfg, cfg),
            recorder=recorder,
        )
        logging.info("plan: %d direction(s)", len(directions))
        if history is not None:
            # Created even with nothing to say, so a human can find where to reply.
            append_divider(history)

    _invalidate_revision(revised)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(plan_to_dict(directions), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("plan: wrote %s", output)
