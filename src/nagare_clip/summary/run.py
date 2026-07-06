"""summary stage: project-wide per-part + all-videos summaries.

When ``summary.enabled`` is false (default) it writes an empty artifact so the
downstream stages are no-ops and the pipeline behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.director.director_llm import clean_for_display
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.summary.summarize import (
    ProjectSummary,
    build_summary,
    summary_to_dict,
)
from nagare_clip.timing import segment_times


def _stem_from_edits(path: Path) -> str:
    name = path.name
    suffix = "_edits.txt"
    return name[: -len(suffix)] if name.endswith(suffix) else path.stem


def run_summary(
    edits_txts: list[Path],
    output: Path,
    cfg: dict,
    *,
    json_paths: list[Path] | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    summary_cfg = cfg["summary"]

    if not summary_cfg.get("enabled", False):
        logging.info("summary: disabled, writing empty summary")
        project = ProjectSummary(summary="", parts=[])
    else:
        parts_input = []
        for path in edits_txts:
            stem = _stem_from_edits(path)
            clean_lines = clean_for_display(path.read_text(encoding="utf-8").splitlines())
            parts_input.append((stem, clean_lines))
        seg_times_by_stem = {}
        for jpath in json_paths or []:
            if jpath.is_file():
                try:
                    seg_times_by_stem[jpath.stem] = segment_times(
                        json.loads(jpath.read_text(encoding="utf-8"))
                    )
                except (ValueError, OSError):
                    logging.warning("summary: could not read --json %s", jpath)
        logging.info("summary: analysing %d video(s) with LLM", len(parts_input))
        project = build_summary(
            parts_input,
            summary_cfg,
            recorder=recorder,
            seg_times_by_stem=seg_times_by_stem or None,
        )
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
