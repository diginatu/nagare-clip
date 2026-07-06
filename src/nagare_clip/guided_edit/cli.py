"""guided_edit stage CLI (Pass B2).

Reads the post-text_filter ``{stem}_edits.txt`` and the director's
``{stem}_director.json``, applies each operation with a small local LLM
(inserting <cut>/<speed>/<overlay>/<keep> tags and {{old->new}} patches at the
precise position), verifies every op deterministically, and writes the
augmented ``{stem}_edits.txt``.

When ``guided_edit.enabled`` is false (default) it copies the input edits
through unchanged so the pipeline behaves exactly as before.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.guided_edit.run import run_guided_edit
from nagare_clip.llm_report import recorder_from_config
from nagare_clip.logging_setup import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="guided_edit stage: apply director ops into _edits.txt."
    )
    parser.add_argument("--edits-txt", required=True, dest="edits_txt")
    parser.add_argument("--director", required=True, dest="director_json")
    parser.add_argument("--output", required=True, dest="output")
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="WhisperX JSON for a final check_edits pass (optional)",
    )
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument(
        "--llm-report-dir",
        dest="llm_report_dir",
        default=None,
        help="Override directory for LLM report output",
    )
    parser.add_argument(
        "--llm-report-no-clear",
        action="store_true",
        dest="llm_report_no_clear",
        help="Do not wipe this stage's report subdir at startup (for per-source loop iterations after the first)",
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

    recorder = recorder_from_config("guided_edit", cfg, override_dir=args.llm_report_dir)
    if not args.llm_report_no_clear:
        recorder.clear()

    run_guided_edit(
        edits_txt=Path(args.edits_txt),
        director_json=Path(args.director_json),
        output=Path(args.output),
        cfg=cfg,
        json_path=Path(args.json_path) if args.json_path else None,
        recorder=recorder,
    )
    recorder.rebuild_index()


if __name__ == "__main__":
    main()
