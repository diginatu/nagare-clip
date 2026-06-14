"""summary stage CLI (project-wide).

Reads every video's post-text_filter ``{stem}_edits.txt`` (repeated
``--edits-txt``), segments each into summarised line-range parts, synthesises one
all-videos summary, and writes a single ``summary.json`` for human review and for
the ``plan`` / ``director`` stages.

When ``summary.enabled`` is false (default) it writes an empty artifact so the
downstream stages are no-ops and the pipeline behaves exactly as before.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.director.director_llm import clean_for_display
from nagare_clip.logging_setup import setup_logging
from nagare_clip.summary.summarize import (
    ProjectSummary,
    build_summary,
    summary_to_dict,
)


def _stem_from_edits(path: Path) -> str:
    name = path.name
    suffix = "_edits.txt"
    return name[: -len(suffix)] if name.endswith(suffix) else path.stem


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="summary stage: project-wide per-part + all-videos summaries."
    )
    parser.add_argument(
        "--edits-txt",
        required=True,
        action="append",
        dest="edits_txt",
        help="Input _edits.txt path (repeat once per source video)",
    )
    parser.add_argument(
        "--output", required=True, dest="output", help="Output summary.json path"
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

    summary_cfg = cfg["summary"]
    output = Path(args.output)

    if not summary_cfg.get("enabled", False):
        logging.info("summary: disabled, writing empty summary")
        project = ProjectSummary(summary="", parts=[])
    else:
        parts_input = []
        for raw in args.edits_txt:
            path = Path(raw)
            stem = _stem_from_edits(path)
            clean_lines = clean_for_display(
                path.read_text(encoding="utf-8").splitlines()
            )
            parts_input.append((stem, clean_lines))
        logging.info("summary: analysing %d video(s) with LLM", len(parts_input))
        project = build_summary(parts_input, summary_cfg)
        logging.info(
            "summary: %d part(s) across %d video(s)",
            len(project.parts),
            len(parts_input),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary_to_dict(project), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("summary: wrote %s", output)


if __name__ == "__main__":
    main()
