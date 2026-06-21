"""text_filter CLI: text editing checkpoint for WhisperX transcriptions.

Produces ``{stem}_edits.txt`` — either a plain copy of the transcription ``.txt``
(when LLM is disabled) or LLM-filtered text with ``{{old->new}}`` markers
preserved for human review.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.llm_report import recorder_from_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.text_filter.llm_filter import filter_transcript
from nagare_clip.text_filter.rule_filter import remove_midstream_closing
from nagare_clip.text_filter.summary_llm import SummaryResult, build_enhanced_prompt, generate_summary


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
        "--llm-report-no-clear", action="store_true", dest="llm_report_no_clear",
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

    # Read input
    lines = txt_path.read_text(encoding="utf-8").splitlines()

    # Rule filter — mark hallucinated closing phrases with {{->}} markers
    original_lines = lines
    lines = remove_midstream_closing(lines)
    rule_changes = sum(1 for o, r in zip(original_lines, lines) if o != r)
    if rule_changes:
        logging.info("text_filter: rule filter marked %d line(s)", rule_changes)

    if not s2["use_llm"]:
        logging.info("text_filter: AI filter disabled, writing edits file")
        result_lines = lines
    else:
        logging.info("text_filter: filtering %d lines with AI", len(lines))

        recorder = recorder_from_config("text_filter", cfg, override_dir=args.llm_report_dir)
        if not args.llm_report_no_clear:
            recorder.clear()

        # Summary LLM — generate context for the filter LLM
        filter_cfg = dict(s2)
        summary_cfg = s2.get("summary_llm", {})
        constant_keywords: list = summary_cfg.get("keywords", [])
        if summary_cfg.get("enabled", False):
            summary_result = generate_summary("\n".join(lines), summary_cfg, recorder=recorder)
            if summary_result is not None:
                summary_result.keywords = constant_keywords + summary_result.keywords
                filter_cfg["prompt"] = build_enhanced_prompt(
                    s2.get("prompt", ""), summary_result
                )
                logging.info(
                    "text_filter: summary generated, %d keywords",
                    len(summary_result.keywords),
                )
            elif constant_keywords:
                filter_cfg["prompt"] = build_enhanced_prompt(
                    s2.get("prompt", ""),
                    SummaryResult(summary="", keywords=constant_keywords),
                )
        elif constant_keywords:
            filter_cfg["prompt"] = build_enhanced_prompt(
                s2.get("prompt", ""),
                SummaryResult(summary="", keywords=constant_keywords),
            )

        # AI filter — returns lines with {{old->new}} markers preserved
        result_lines = filter_transcript(lines, filter_cfg, recorder=recorder)

        # Count changes
        changes = sum(1 for o, c in zip(lines, result_lines) if o != c)
        logging.info("text_filter: %d/%d lines modified by AI", changes, len(lines))

        recorder.rebuild_index()

    # Write output
    output_txt.parent.mkdir(parents=True, exist_ok=True)
    output_txt.write_text("\n".join(result_lines) + "\n", encoding="utf-8")

    logging.info("text_filter: wrote %s", output_txt)


if __name__ == "__main__":
    main()
