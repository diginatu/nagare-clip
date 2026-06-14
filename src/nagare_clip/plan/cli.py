"""plan stage CLI (project-wide).

Reads the ``summary.json`` produced by the summary stage and asks a larger LLM
for a coarse, cross-video editorial direction per part, writing ``plan.json`` for
human review and for the per-video ``director`` stage.

When ``plan.enabled`` is false (default) it writes an empty artifact so the
downstream director is unaffected and the pipeline behaves exactly as before.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.llm_report import recorder_from_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.plan.plan_llm import generate_plan, plan_to_dict
from nagare_clip.summary.summarize import summary_from_dict


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="plan stage: coarse cross-video rough directions per part."
    )
    parser.add_argument(
        "--summary",
        required=True,
        dest="summary",
        help="Input summary.json path (from the summary stage)",
    )
    parser.add_argument(
        "--output", required=True, dest="output", help="Output plan.json path"
    )
    parser.add_argument(
        "--config", dest="config_path", default=None, help="Path to YAML config file"
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--llm-report-dir", default=None, dest="llm_report_dir")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cli_overrides: dict = {}
    if args.log_level is not None:
        cli_overrides.setdefault("general", {})["log_level"] = args.log_level

    config_path = Path(args.config_path) if args.config_path else None
    cfg = get_effective_config(config_path, cli_overrides)
    setup_logging(
        cfg["general"]["log_level"],
        args.log_file or cfg["general"]["log_file"] or None,
    )

    recorder = recorder_from_config("plan", cfg, override_dir=args.llm_report_dir)
    recorder.clear()

    plan_cfg = cfg["plan"]
    output = Path(args.output)

    if not plan_cfg.get("enabled", False):
        logging.info("plan: disabled, writing empty plan")
        directions = []
    else:
        summary_path = Path(args.summary)
        project_summary = summary_from_dict(
            json.loads(summary_path.read_text(encoding="utf-8"))
        )
        logging.info("plan: directing %d part(s) with LLM", len(project_summary.parts))
        directions = generate_plan(project_summary, plan_cfg, recorder=recorder)
        logging.info("plan: %d direction(s)", len(directions))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(plan_to_dict(directions), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("plan: wrote %s", output)
    recorder.rebuild_index()


if __name__ == "__main__":
    main()
