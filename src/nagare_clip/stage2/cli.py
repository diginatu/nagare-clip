"""Stage 2 CLI: text editing checkpoint for WhisperX transcriptions.

Produces ``{stem}_edits.txt`` — either a plain copy of the Stage 1 ``.txt``
(when LLM is disabled) or LLM-filtered text with ``{{old->new}}`` markers
preserved for human review.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.stage2.llm_filter import filter_transcript


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Text editing checkpoint for WhisperX transcriptions."
    )
    parser.add_argument(
        "--txt", required=True, dest="txt_path", help="WhisperX .txt path"
    )
    parser.add_argument(
        "--output-txt",
        required=True,
        dest="output_txt",
        help="Output edits .txt path",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    cli_overrides: dict = {}
    if args.log_level is not None:
        cli_overrides.setdefault("general", {})["log_level"] = args.log_level

    config_path = Path(args.config_path) if args.config_path else None
    cfg = get_effective_config(config_path, cli_overrides)

    logging.basicConfig(
        level=cfg["general"]["log_level"], format="%(levelname)s: %(message)s"
    )

    s2 = cfg["stage2"]

    txt_path = Path(args.txt_path)
    output_txt = Path(args.output_txt)

    if not s2["use_llm"]:
        logging.info("Stage 2: LLM disabled, copying .txt to edits file")
        output_txt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(txt_path, output_txt)
        return

    # Read input
    lines = txt_path.read_text(encoding="utf-8").splitlines()

    logging.info("Stage 2: filtering %d lines with LLM", len(lines))

    # Filter text — returns lines with {{old->new}} markers preserved
    result_lines = filter_transcript(lines, s2)

    # Count changes
    changes = sum(1 for o, c in zip(lines, result_lines) if o != c)
    logging.info("Stage 2: %d/%d lines modified by LLM", changes, len(lines))

    # Write output
    output_txt.parent.mkdir(parents=True, exist_ok=True)
    output_txt.write_text("\n".join(result_lines) + "\n", encoding="utf-8")

    logging.info("Stage 2: wrote %s", output_txt)


if __name__ == "__main__":
    main()
