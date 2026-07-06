"""plan stage CLI (project-wide).

Reads the ``summary.json`` produced by the summary stage and asks a larger LLM
for a coarse, cross-video editorial direction per part, writing ``plan.json`` for
human review and for the per-video ``director`` stage.

When ``plan.enabled`` is false (default) it writes an empty artifact so the
downstream director is unaffected and the pipeline behaves exactly as before.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.llm_report import recorder_from_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.plan.run import run_plan


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
    parser.add_argument("--output", required=True, dest="output", help="Output plan.json path")
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
    parser.add_argument(
        "--llm-report-no-clear",
        action="store_true",
        dest="llm_report_no_clear",
        help="Do not wipe this stage's report subdir at startup (for per-source loop iterations after the first)",
    )
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
    if not args.llm_report_no_clear:
        recorder.clear()

    try:
        run_plan(
            Path(args.summary),
            Path(args.output),
            cfg,
            recorder=recorder,
        )
    finally:
        recorder.rebuild_index()


if __name__ == "__main__":
    main()
