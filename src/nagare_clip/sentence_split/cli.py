"""sentence_split stage CLI (per source).

Re-segments a WhisperX ``{stem}.json`` into one-sentence-per-line segments and
writes the re-segmented ``.json`` plus a matching ``.txt`` (one segment per
line).  When ``sentence_split.enabled`` is false it copies the transcription
``.json``/``.txt`` through byte-identically, so downstream behaviour is
unchanged.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from nagare_clip.config import get_effective_config
from nagare_clip.llm_report import recorder_from_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.sentence_split.run import run_sentence_split


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="sentence_split stage: LLM re-segmentation of a WhisperX transcript."
    )
    parser.add_argument("--json", required=True, dest="json", help="Input WhisperX JSON")
    parser.add_argument("--txt", required=True, dest="txt", help="Input transcript .txt")
    parser.add_argument("--output-json", required=True, dest="output_json")
    parser.add_argument("--output-txt", required=True, dest="output_txt")
    parser.add_argument("--stem", default="", dest="stem")
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--llm-report-dir", default=None, dest="llm_report_dir")
    parser.add_argument("--llm-report-no-clear", action="store_true", dest="llm_report_no_clear")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cli_overrides: dict[str, Any] = {}
    if args.log_level is not None:
        cli_overrides.setdefault("general", {})["log_level"] = args.log_level
    cfg = get_effective_config(Path(args.config_path) if args.config_path else None, cli_overrides)
    setup_logging(cfg["general"]["log_level"], args.log_file or cfg["general"]["log_file"] or None)
    recorder = recorder_from_config("sentence_split", cfg, override_dir=args.llm_report_dir)
    if not args.llm_report_no_clear:
        recorder.clear()

    try:
        run_sentence_split(
            Path(args.json),
            Path(args.txt),
            Path(args.output_json),
            Path(args.output_txt),
            cfg,
            stem=args.stem,
            recorder=recorder,
        )
    finally:
        recorder.rebuild_index()


if __name__ == "__main__":
    main()
