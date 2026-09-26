"""guided_edit stage (Pass B2): apply director ops into _edits.txt.

The output carries the director's silence lines (:mod:`nagare_clip.edit_lines`)
— the waits it was shown as lines of their own, in the very text it read — so
an op on ``"n~"`` lands as an ordinary marker on the silence line after ``n``
and ``_edits.txt`` stays the one record of every edit.  They are written
whether or not the stage is enabled (a human can ``<keep>`` a silence without
the director); when ``guided_edit.enabled`` is false (default) no op is
applied.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.director.director_llm import ops_from_dict
from nagare_clip.director.silence_lines import build_silence_lines
from nagare_clip.edit_lines import (
    gap_spans,
    insert_silence_lines,
    parse_edit_lines,
    silence_line_min,
)
from nagare_clip.gap_context.context import anchor_gaps
from nagare_clip.gap_context.gaps import load_gaps
from nagare_clip.guided_edit.apply import apply_ops
from nagare_clip.guided_edit.timelapse import expand_timelapse_ops
from nagare_clip.intervals.check_edits import check_edits
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.timing import segment_times


def with_silence_lines(
    edit_lines: list[str],
    json_data: dict | None,
    min_seconds: float,
    gaps_path: Path | None = None,
) -> list[str]:
    """*edit_lines* with the director's silence lines written in.

    The set and the text are the director's own: :func:`build_silence_lines`
    over the whole source, each rendered by :meth:`SilenceLine.body` — the
    line the director's view numbers.  A file that already has silence lines
    is returned as it is; without timings there is nothing to write.
    """
    if json_data is None or parse_edit_lines(edit_lines).silences():
        return list(edit_lines)
    anchored = anchor_gaps(load_gaps(gaps_path), segment_times(json_data)) if gaps_path else []
    silences, _ = build_silence_lines(json_data, anchored, min_seconds=min_seconds)
    return insert_silence_lines(edit_lines, {s.after_line: s.body() for s in silences})


def run_guided_edit(
    edits_txt: Path,
    director_json: Path,
    output: Path,
    cfg: dict,
    *,
    json_path: Path | None = None,
    gaps_path: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    ge_cfg = cfg["guided_edit"]
    stem = output.stem.replace("_edits", "")
    min_seconds = silence_line_min(cfg.get("director", {}))

    # Read once: the timings decide the silence lines, size a timelapse's
    # caption, and drive the closing check_edits pass.
    json_data = json.loads(json_path.read_text(encoding="utf-8")) if json_path else None
    edit_lines = with_silence_lines(
        edits_txt.read_text(encoding="utf-8").splitlines(), json_data, min_seconds, gaps_path
    )

    if not ge_cfg.get("enabled", False):
        logging.info("guided_edit: disabled, copying edits through (silence lines added)")
        result_lines = edit_lines
        unapplied: list = []
    else:
        director_data = json.loads(director_json.read_text(encoding="utf-8"))
        speech_count = len(parse_edit_lines(edit_lines).speech_lines())
        ops = ops_from_dict(director_data, num_lines=speech_count)
        ops = expand_timelapse_ops(
            ops,
            segment_times(json_data) if json_data else [],
            gap_spans(json_data) if json_data else {},
        )
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
        problems = check_edits(result_lines, json_data, silence_line_min=min_seconds)
        for p in problems:
            where = "file" if p.line is None else f"line {p.line}"
            logging.warning("check_edits: %s: %s", where, p.message)
        if problems:
            logging.warning("guided_edit: %d check_edits problem(s) in output", len(problems))
