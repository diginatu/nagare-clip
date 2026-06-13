"""guided_edit stage CLI (Pass B2).

Reads the post-text_filter ``{stem}_edits.txt`` and the director's
``{stem}_director.json``, applies each operation with a small local LLM
(inserting <cut>/<speed>/<overlay>/<keep> tags and {{old->new}} patches at the
precise position), verifies every op deterministically, and writes the
augmented ``{stem}_edits.txt`` plus a ``{stem}_unapplied.txt`` report.

When ``guided_edit.enabled`` is false (default) it copies the input edits
through unchanged so the pipeline behaves exactly as before.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.director.director_llm import ops_from_dict
from nagare_clip.guided_edit.apply import apply_ops, format_unapplied
from nagare_clip.logging_setup import setup_logging
from nagare_clip.stage3.check_edits import check_edits


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="guided_edit stage: apply director ops into _edits.txt."
    )
    parser.add_argument("--edits-txt", required=True, dest="edits_txt")
    parser.add_argument("--director", required=True, dest="director_json")
    parser.add_argument("--output", required=True, dest="output")
    parser.add_argument(
        "--unapplied",
        dest="unapplied",
        default=None,
        help="Unapplied-ops report path (default: <output dir>/<stem>_unapplied.txt)",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="WhisperX JSON for a final check_edits pass (optional)",
    )
    parser.add_argument("--config", dest="config_path", default=None)
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

    ge_cfg = cfg["guided_edit"]
    edit_lines = Path(args.edits_txt).read_text(encoding="utf-8").splitlines()
    output = Path(args.output)
    unapplied_path = (
        Path(args.unapplied)
        if args.unapplied
        else output.with_name(output.stem.replace("_edits", "") + "_unapplied.txt")
    )

    if not ge_cfg.get("enabled", False):
        logging.info("guided_edit: disabled, copying edits through")
        result_lines = edit_lines
        unapplied: list = []
    else:
        director_data = json.loads(
            Path(args.director_json).read_text(encoding="utf-8")
        )
        ops = ops_from_dict(director_data, num_lines=len(edit_lines))
        logging.info("guided_edit: applying %d director op(s)", len(ops))
        result_lines, unapplied = apply_ops(edit_lines, ops, ge_cfg)
        logging.info(
            "guided_edit: %d applied, %d unapplied",
            len(ops) - len(unapplied),
            len(unapplied),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(result_lines) + "\n", encoding="utf-8")
    unapplied_path.write_text(format_unapplied(unapplied), encoding="utf-8")
    logging.info("guided_edit: wrote %s", output)

    if args.json_path:
        with open(args.json_path, "r", encoding="utf-8") as f:
            json_data = json.load(f)
        problems = check_edits(result_lines, json_data)
        for p in problems:
            where = "file" if p.line is None else f"line {p.line}"
            logging.warning("check_edits: %s: %s", where, p.message)
        if problems:
            logging.warning(
                "guided_edit: %d check_edits problem(s) in output", len(problems)
            )


if __name__ == "__main__":
    main()
