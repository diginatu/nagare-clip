"""guided_edit stage (Pass B2): apply director ops into _edits.txt.

When ``guided_edit.enabled`` is false (default) it copies the input edits
through unchanged so the pipeline behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.director.director_llm import ops_from_dict
from nagare_clip.guided_edit.apply import apply_ops
from nagare_clip.guided_edit.timelapse import expand_timelapse_ops
from nagare_clip.intervals.check_edits import check_edits
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.timing import segment_times


def run_guided_edit(
    edits_txt: Path,
    director_json: Path,
    output: Path,
    cfg: dict,
    *,
    json_path: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    ge_cfg = cfg["guided_edit"]
    edit_lines = edits_txt.read_text(encoding="utf-8").splitlines()
    stem = output.stem.replace("_edits", "")

    # Read once: the segment times size a timelapse's caption, and the same
    # data drives the closing check_edits pass.
    json_data = json.loads(json_path.read_text(encoding="utf-8")) if json_path else None

    if not ge_cfg.get("enabled", False):
        logging.info("guided_edit: disabled, copying edits through")
        result_lines = edit_lines
        unapplied: list = []
    else:
        director_data = json.loads(director_json.read_text(encoding="utf-8"))
        ops = ops_from_dict(director_data, num_lines=len(edit_lines))
        ops = expand_timelapse_ops(ops, segment_times(json_data) if json_data else [])
        logging.info("guided_edit: applying %d director op(s)", len(ops))
        result_lines, unapplied = apply_ops(edit_lines, ops, ge_cfg, recorder=recorder, unit=stem)
        logging.info(
            "guided_edit: %d applied, %d unapplied",
            len(ops) - len(unapplied),
            len(unapplied),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(result_lines) + "\n", encoding="utf-8")
    logging.info("guided_edit: wrote %s", output)

    if json_data is not None:
        problems = check_edits(result_lines, json_data)
        for p in problems:
            where = "file" if p.line is None else f"line {p.line}"
            logging.warning("check_edits: %s: %s", where, p.message)
        if problems:
            logging.warning("guided_edit: %d check_edits problem(s) in output", len(problems))
