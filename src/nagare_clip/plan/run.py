"""plan stage: coarse cross-video rough directions per part.

When ``plan.enabled`` is false (default) it writes an empty artifact so the
downstream director is unaffected and the pipeline behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.plan.plan_llm import generate_plan, plan_to_dict
from nagare_clip.summary.summarize import summary_from_dict


def run_plan(
    summary_json: Path,
    output: Path,
    cfg: dict,
    *,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    plan_cfg = cfg["plan"]

    if not plan_cfg.get("enabled", False):
        logging.info("plan: disabled, writing empty plan")
        directions = []
    else:
        project_summary = summary_from_dict(json.loads(summary_json.read_text(encoding="utf-8")))
        logging.info("plan: directing %d part(s) with LLM", len(project_summary.parts))
        directions = generate_plan(project_summary, plan_cfg, recorder=recorder)
        logging.info("plan: %d direction(s)", len(directions))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(plan_to_dict(directions), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("plan: wrote %s", output)
