"""summary stage: project-wide per-part + all-videos summaries.

Runs before ``text_filter``, reading the sentence_split ``{stem}.txt``
transcripts directly. When ``summary.enabled`` is false (default) it writes an
empty artifact so the downstream stages are no-ops and the pipeline behaves
exactly as before. When enabled, it also emits per-video keywords
(misspelling-prone words) for the ``text_filter`` LLM to consult.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.gap_context.context import anchor_gaps, format_gap_block
from nagare_clip.gap_context.gaps import load_gaps
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.summary import summarize as summarize_mod
from nagare_clip.summary.summarize import (
    ProjectSummary,
    build_summary,
    summary_to_dict,
)
from nagare_clip.timing import segment_times


def run_summary(
    txts: list[Path],
    output: Path,
    cfg: dict,
    *,
    json_paths: list[Path] | None = None,
    gaps_paths: list[Path] | None = None,
    cuts_paths: list[Path] | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    summary_cfg = cfg["summary"]

    if not summary_cfg.get("enabled", False):
        logging.info("summary: disabled, writing empty summary")
        project = ProjectSummary(summary="", parts=[])
    else:
        parts_input = []
        for path in txts:
            lines = path.read_text(encoding="utf-8").splitlines()
            parts_input.append((path.stem, lines))
        seg_times_by_stem = {}
        for jpath in json_paths or []:
            if jpath.is_file():
                try:
                    seg_times_by_stem[jpath.stem] = segment_times(
                        json.loads(jpath.read_text(encoding="utf-8"))
                    )
                except (ValueError, OSError):
                    logging.warning("summary: could not read --json %s", jpath)
        gap_blocks_by_stem: dict[str, str] = {}
        for gpath in gaps_paths or []:
            stem = gpath.stem.removesuffix("_gaps")
            gaps = load_gaps(gpath)
            if not gaps:
                continue
            block = format_gap_block(anchor_gaps(gaps, seg_times_by_stem.get(stem, [])))
            if block:
                gap_blocks_by_stem[stem] = block
        cuts_by_stem: dict[str, list[tuple[float, float]]] = {}
        for cpath in cuts_paths or []:
            if cpath.is_file():
                stem = cpath.stem.removesuffix("_cuts")
                cuts_by_stem[stem] = read_cuts(cpath)
        logging.info("summary: analysing %d video(s) with LLM", len(parts_input))
        project = build_summary(
            parts_input,
            summary_cfg,
            call_llm=summarize_mod._call_llm,
            recorder=recorder,
            seg_times_by_stem=seg_times_by_stem or None,
            gap_blocks_by_stem=gap_blocks_by_stem or None,
            cuts_by_stem=cuts_by_stem or None,
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
