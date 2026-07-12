"""text_filter stage: text editing checkpoint for WhisperX transcriptions.

Produces ``{stem}_edits.txt`` — either a plain copy of the transcription
``.txt`` (when LLM is disabled) or LLM-filtered text with ``{{old->new}}``
markers preserved for human review. When the summary stage is enabled, its
``summary.json`` (per-video part summaries + misspelling-prone keywords)
enriches the filter LLM's system prompt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.summary.summarize import summary_from_dict
from nagare_clip.text_filter.context import build_enhanced_prompt
from nagare_clip.text_filter.llm_filter import filter_transcript
from nagare_clip.text_filter.rule_filter import remove_midstream_closing


def _summary_context(summary_json: Path | None, stem: str) -> tuple[list[str], list[str]]:
    """This stem's (part summaries, keywords) from summary.json; empty on any failure."""
    if summary_json is None or not summary_json.is_file():
        return [], []
    try:
        project = summary_from_dict(json.loads(summary_json.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        logging.warning("text_filter: could not read summary json %s", summary_json)
        return [], []
    summaries = [p.summary for p in project.parts if p.stem == stem]
    return summaries, project.keywords.get(stem, [])


def run_text_filter(
    txt: Path,
    output_txt: Path,
    cfg: dict,
    *,
    summary_json: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    s2 = cfg["text_filter"]
    lines = txt.read_text(encoding="utf-8").splitlines()

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

        filter_cfg = dict(s2)
        summaries, summary_keywords = _summary_context(summary_json, txt.stem)
        keywords = list(dict.fromkeys(list(s2.get("keywords", [])) + summary_keywords))
        if summaries or keywords:
            filter_cfg["prompt"] = build_enhanced_prompt(s2.get("prompt", ""), summaries, keywords)
            logging.info(
                "text_filter: summary context: %d part summaries, %d keyword(s)",
                len(summaries),
                len(keywords),
            )

        result_lines = filter_transcript(lines, filter_cfg, recorder=recorder)

        changes = sum(1 for o, c in zip(lines, result_lines) if o != c)
        logging.info("text_filter: %d/%d lines modified by AI", changes, len(lines))

    output_txt.parent.mkdir(parents=True, exist_ok=True)
    output_txt.write_text("\n".join(result_lines) + "\n", encoding="utf-8")
    logging.info("text_filter: wrote %s", output_txt)
