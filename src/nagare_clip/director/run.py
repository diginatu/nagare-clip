"""director stage (Pass A): high-level LLM edit operations.

When ``director.enabled`` is false (default) it writes an empty op list so the
downstream guided_edit stage is a no-op and the pipeline behaves exactly as
before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.brief import apply_brief
from nagare_clip.director import director_llm as director_llm_mod
from nagare_clip.director.context import build_director_context
from nagare_clip.director.director_llm import generate_director_ops, ops_to_dict
from nagare_clip.gap_context.gaps import load_gaps
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.plan.plan_llm import plan_from_dict
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict
from nagare_clip.timing import segment_silences, segment_times


def _build_overview_context(summary: Path | None, plan: Path | None, stem: str | None) -> str:
    """Load summary/plan artifacts (tolerating missing/empty) and render the
    cross-video context for this video's stem.  Returns ``""`` if unavailable."""
    if not stem:
        return ""
    project_summary = ProjectSummary(summary="", parts=[])
    if summary and summary.is_file():
        project_summary = summary_from_dict(json.loads(summary.read_text(encoding="utf-8")))
    directions = []
    if plan and plan.is_file():
        directions = plan_from_dict(json.loads(plan.read_text(encoding="utf-8")))
    return build_director_context(project_summary, directions, stem)


def run_director(
    edits_txt: Path,
    output: Path,
    cfg: dict,
    *,
    summary: Path | None = None,
    plan: Path | None = None,
    stem: str | None = None,
    json_path: Path | None = None,
    gaps: Path | None = None,
    cuts_txt: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    director_cfg = cfg["director"]
    edit_lines = edits_txt.read_text(encoding="utf-8").splitlines()
    unit = stem or output.stem.replace("_director", "")

    if not director_cfg.get("enabled", False):
        logging.info("director: disabled, writing empty op list")
        ops = []
    else:
        overview_context = _build_overview_context(summary, plan, stem)
        seg_times = None
        if json_path and json_path.is_file():
            try:
                seg_times = segment_times(json.loads(json_path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                logging.warning("director: could not read --json %s", json_path)
        silences = None
        if seg_times and cuts_txt and Path(cuts_txt).is_file():
            silences = segment_silences(seg_times, read_cuts(Path(cuts_txt)))
        gap_list = load_gaps(gaps)
        logging.info("director: analysing %d line(s) with LLM", len(edit_lines))
        ops = generate_director_ops(
            edit_lines,
            apply_brief(director_cfg, cfg),
            call_llm=director_llm_mod._call_llm,
            overview_context=overview_context,
            recorder=recorder,
            unit=unit,
            seg_times=seg_times,
            gaps=gap_list,
            silences=silences,
        )
        logging.info("director: %d operation(s)", len(ops))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(ops_to_dict(ops), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("director: wrote %s", output)
