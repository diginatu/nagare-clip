"""director stage CLI (Pass A).

Reads a post-text_filter ``{stem}_edits.txt``, asks a larger LLM for
high-level edit operations (cut / speed / overlay / keep / edit) referenced by
line number, and writes them to ``{stem}_director.json`` for human review.

When ``director.enabled`` is false (default) it writes an empty op list so the
downstream guided_edit stage is a no-op and the pipeline behaves exactly as
before.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.director.context import build_director_context
from nagare_clip.director.director_llm import generate_director_ops, ops_to_dict
from nagare_clip.llm_report import recorder_from_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.plan.plan_llm import plan_from_dict
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="director stage: LLM high-level edit operations."
    )
    parser.add_argument(
        "--edits-txt", required=True, dest="edits_txt", help="Input _edits.txt path"
    )
    parser.add_argument(
        "--output", required=True, dest="output", help="Output _director.json path"
    )
    parser.add_argument(
        "--summary",
        dest="summary",
        default=None,
        help="Optional summary.json (cross-video context)",
    )
    parser.add_argument(
        "--plan",
        dest="plan",
        default=None,
        help="Optional plan.json (cross-video rough directions)",
    )
    parser.add_argument(
        "--stem",
        dest="stem",
        default=None,
        help="This video's stem (to select its parts/directions from the overview)",
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
    parser.add_argument(
        "--llm-report-no-clear", action="store_true", dest="llm_report_no_clear",
        help="Do not wipe this stage's report subdir at startup (for per-source loop iterations after the first)",
    )
    return parser.parse_args()


def _build_overview_context(args: argparse.Namespace) -> str:
    """Load summary/plan artifacts (tolerating missing/empty) and render the
    cross-video context for this video's stem.  Returns ``""`` if unavailable."""
    if not args.stem:
        return ""
    project_summary = ProjectSummary(summary="", parts=[])
    if args.summary and Path(args.summary).is_file():
        project_summary = summary_from_dict(
            json.loads(Path(args.summary).read_text(encoding="utf-8"))
        )
    directions = []
    if args.plan and Path(args.plan).is_file():
        directions = plan_from_dict(
            json.loads(Path(args.plan).read_text(encoding="utf-8"))
        )
    return build_director_context(project_summary, directions, args.stem)


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

    recorder = recorder_from_config("director", cfg, override_dir=args.llm_report_dir)
    if not args.llm_report_no_clear:
        recorder.clear()

    director_cfg = cfg["director"]
    edit_lines = Path(args.edits_txt).read_text(encoding="utf-8").splitlines()
    output = Path(args.output)
    stem = args.stem or output.stem.replace("_director", "")

    if not director_cfg.get("enabled", False):
        logging.info("director: disabled, writing empty op list")
        ops = []
    else:
        overview_context = _build_overview_context(args)
        logging.info("director: analysing %d line(s) with LLM", len(edit_lines))
        ops = generate_director_ops(
            edit_lines, director_cfg, overview_context=overview_context,
            recorder=recorder, unit=stem,
        )
        logging.info("director: %d operation(s)", len(ops))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(ops_to_dict(ops), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("director: wrote %s", output)
    recorder.rebuild_index()


if __name__ == "__main__":
    main()
