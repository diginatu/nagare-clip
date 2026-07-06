"""text_filter stage: text editing checkpoint for WhisperX transcriptions.

Produces ``{stem}_edits.txt`` — either a plain copy of the transcription
``.txt`` (when LLM is disabled) or LLM-filtered text with ``{{old->new}}``
markers preserved for human review.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.text_filter.llm_filter import filter_transcript
from nagare_clip.text_filter.rule_filter import remove_midstream_closing
from nagare_clip.text_filter.summary_llm import (
    SummaryResult,
    build_enhanced_prompt,
    generate_summary,
)


def run_text_filter(
    txt: Path,
    output_txt: Path,
    cfg: dict,
    *,
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

        # Summary LLM — generate context for the filter LLM
        filter_cfg = dict(s2)
        summary_cfg = s2.get("summary_llm", {})
        constant_keywords: list = summary_cfg.get("keywords", [])
        if summary_cfg.get("enabled", False):
            summary_result = generate_summary("\n".join(lines), summary_cfg, recorder=recorder)
            if summary_result is not None:
                summary_result.keywords = constant_keywords + summary_result.keywords
                filter_cfg["prompt"] = build_enhanced_prompt(s2.get("prompt", ""), summary_result)
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

        changes = sum(1 for o, c in zip(lines, result_lines) if o != c)
        logging.info("text_filter: %d/%d lines modified by AI", changes, len(lines))

    output_txt.parent.mkdir(parents=True, exist_ok=True)
    output_txt.write_text("\n".join(result_lines) + "\n", encoding="utf-8")
    logging.info("text_filter: wrote %s", output_txt)
