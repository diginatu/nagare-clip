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
from nagare_clip.director.director_llm import generate_director_ops, ops_to_dict
from nagare_clip.logging_setup import setup_logging


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

    director_cfg = cfg["director"]
    edit_lines = Path(args.edits_txt).read_text(encoding="utf-8").splitlines()
    output = Path(args.output)

    if not director_cfg.get("enabled", False):
        logging.info("director: disabled, writing empty op list")
        ops = []
    else:
        logging.info("director: analysing %d line(s) with LLM", len(edit_lines))
        ops = generate_director_ops(edit_lines, director_cfg)
        logging.info("director: %d operation(s)", len(ops))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(ops_to_dict(ops), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("director: wrote %s", output)


if __name__ == "__main__":
    main()
