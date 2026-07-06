"""text_filter CLI: text editing checkpoint for WhisperX transcriptions.

Produces ``{stem}_edits.txt`` — either a plain copy of the transcription ``.txt``
(when LLM is disabled) or LLM-filtered text with ``{{old->new}}`` markers
preserved for human review.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.llm_report import NULL_RECORDER, recorder_from_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.text_filter.run import run_text_filter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Text editing checkpoint for WhisperX transcriptions."
    )
    parser.add_argument("--txt", required=True, dest="txt_path", help="WhisperX .txt path")
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
    parser.add_argument(
        "--log-file",
        default=None,
        help="Path to log file; appends to existing file (default: console only)",
    )
    parser.add_argument(
        "--llm-report-dir",
        default=None,
        dest="llm_report_dir",
        help="Directory for LLM report output (overrides config)",
    )
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

    s2 = cfg["text_filter"]

    txt_path = Path(args.txt_path)
    output_txt = Path(args.output_txt)

    # Create recorder only when use_llm is true
    if s2["use_llm"]:
        recorder = recorder_from_config("text_filter", cfg, override_dir=args.llm_report_dir)
        if not args.llm_report_no_clear:
            recorder.clear()
    else:
        recorder = NULL_RECORDER

    # Run text_filter
    run_text_filter(txt_path, output_txt, cfg, recorder=recorder)

    # Rebuild index if recorder was used
    if s2["use_llm"]:
        recorder.rebuild_index()


if __name__ == "__main__":
    main()
