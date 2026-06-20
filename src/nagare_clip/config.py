"""Centralised YAML configuration loading and merging."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULTS: Dict[str, Any] = {
    "general": {
        "log_level": "INFO",
        "log_file": "",
        "llm_report": True,
        "llm_report_dir": "output/llm_report",
    },
    "transcription": {
        "compute_type": "float16",
        "batch_size": 16,
        "align_model": "",
        "language": "ja",
    },
    "text_filter": {
        "use_llm": False,
        "provider": "ollama_chat",
        "api_base": "",
        "model": "qwen3.5:4b",
        "api_key": "",
        "batch_size": 10,
        "timeout": 60,
        "retry_on_invalid": True,
        "retry_min_batch_size": 1,
        "prompt": (
            "Fix speech recognition errors in Japanese text.\n"
            "Remove filler words (あのー, えーと) and noise like (雑音).\n"
            "Only fix clear mistakes. Do NOT rephrase correct text.\n"
            "\n"
            "Rules:\n"
            "- Copy each line fully with its number.\n"
            "- Wrap ONLY the erroneous part: {{error->fix}} or {{delete->}}.\n"
            "- Keep all surrounding text unchanged.\n"
            "\n"
            "Example:\n"
            "Input:\n"
            "1: えーとそれは急はいい天気ですね\n"
            "2: 正しい文です\n"
            "3: (雑音)\n"
            "\n"
            "Output:\n"
            "1: {{えーと->}}それは{{急は->今日は}}いい天気ですね\n"
            "2: 正しい文です\n"
            "3: {{(雑音)->}}"
        ),
        "temperature": 0.1,
        "thinking": False,
        "summary_llm": {
            "enabled": False,
            "keywords": [],
            "provider": "ollama_chat",
            "api_base": "",
            "model": "qwen3.5:4b",
            "api_key": "",
            "temperature": 0.3,
            "thinking": False,
            "timeout": 120,
            "response_format": "json",
            "prompt": (
                "Analyze the following Japanese transcript from a video.\n"
                "Provide a JSON object with:\n"
                '- "summary": A very short summary (1-2 sentences) of the content.\n'
                '- "keywords": A list of rare or domain-specific words that speech '
                "recognition might misspell.\n"
                "\n"
                "Output only the JSON object, no other text."
            ),
        },
    },
    # "summary" stage: runs once project-wide before "director".  A larger LLM
    # segments each video's numbered transcript into line-range parts and writes
    # a summary per part; a reduce step then synthesises one all-videos summary.
    # Output is a reviewable summary.json consumed by "plan" and "director".
    # Disabled by default (writes an empty summary = no-op).
    "summary": {
        "enabled": False,
        "provider": "ollama_chat",
        "api_base": "",
        "model": "gpt-oss:120b",
        "api_key": "",
        "temperature": 0.3,
        "thinking": False,
        "timeout": 300,
        "response_format": "json",
        "max_retries": 2,
        "retry_temp_step": 0.2,
        "retry_temp_cap": 0.8,
        "prompt": (
            "You are a video editor. You receive ONE Japanese transcript as "
            "numbered lines (one line per subtitle segment). Split it into a few "
            "contiguous PARTS by topic/section and summarise each part. Reference "
            "lines by their 1-based numbers (inclusive). Output ONLY a JSON object.\n"
            "\n"
            "JSON shape:\n"
            '{"parts": [\n'
            '  {"lines": [1, 12], "summary": "what this part covers"},\n'
            '  {"lines": [13, 40], "summary": "..."}\n'
            "]}\n"
            "\n"
            "Rules:\n"
            "- Parts must be contiguous and within the transcript range.\n"
            "- Keep each summary to one short sentence.\n"
            "- Output only the JSON object, no other text."
        ),
        "overall_prompt": (
            "You are a video editor. You receive numbered per-part summaries "
            "spanning several source videos of one project. Write ONE concise "
            "overall summary of the whole project. Output ONLY a JSON object:\n"
            '{"summary": "..."}\n'
            "Output only the JSON object, no other text."
        ),
    },
    # "plan" stage: runs once project-wide after "summary", before "director".
    # A larger LLM reads the per-part summaries (with line ranges) of all videos
    # and gives a coarse, cross-video editorial direction per part (e.g. remove /
    # shorten / speed / keep).  Output is a reviewable plan.json consumed by
    # "director".  Disabled by default (writes an empty plan = no-op).
    "plan": {
        "enabled": False,
        "provider": "ollama_chat",
        "api_base": "",
        "model": "gpt-oss:120b",
        "api_key": "",
        "temperature": 0.3,
        "thinking": False,
        "timeout": 300,
        "response_format": "json",
        "max_retries": 2,
        "retry_temp_step": 0.2,
        "retry_temp_cap": 0.8,
        "prompt": (
            "You are a video editor planning a rough cut across several source "
            "videos. You receive numbered PARTS (each with a source video, a line "
            "range, and a summary) plus an overall summary. For each part, give a "
            "ROUGH editorial direction — what to do with it (e.g. remove, shorten, "
            "speed up, keep, emphasise) and why, considering the whole project "
            "(e.g. a part that repeats an earlier one can be removed). Reference "
            "parts by their 1-based index. Output ONLY a JSON object.\n"
            "\n"
            "By default, non-speech stretches are dropped. \"keep\" preserves ALL "
            "content in the range (silences and non-speech gaps included) — use "
            "it when those moments matter.\n"
            "\n"
            "JSON shape:\n"
            '{"directions": [\n'
            '  {"index": 1, "direction": "keep — the product\'s operating noise '
            'is the point"},\n'
            '  {"index": 2, "direction": "remove — repeats part 1"}\n'
            "]}\n"
            "\n"
            "Rules:\n"
            '- "index" must be one of the given part numbers.\n'
            "- Keep each direction to one short, actionable phrase.\n"
            "- Output only the JSON object, no other text."
        ),
    },
    # Pipeline "director" stage (Pass A): a larger LLM reads the whole
    # numbered transcript and emits a JSON list of high-level edit operations
    # (cut / speed / overlay / keep / edit) referenced by line number.  It
    # never re-outputs the transcript text.  Disabled by default (no-op).
    "director": {
        "enabled": False,
        "provider": "ollama_chat",
        "api_base": "",
        "model": "gpt-oss:120b",
        "api_key": "",
        "temperature": 0.2,
        "thinking": False,
        "timeout": 300,
        "response_format": "json",
        "max_retries": 2,
        "retry_temp_step": 0.2,
        "retry_temp_cap": 0.8,
        "prompt": (
            "You are a video editor. You receive a Japanese transcript as "
            "numbered lines (one line per subtitle segment). Decide high-level "
            "edits to tighten the video. Do NOT rewrite or output the "
            "transcript text. Output ONLY a JSON object.\n"
            "\n"
            "Operations (reference lines by their 1-based numbers, inclusive):\n"
            "- cut: remove a boring/redundant span entirely (deletes audio+video).\n"
            '- speed: play a span faster; give "factor" (e.g. 2.0). Internal silences/pauses are still dropped — add a "keep" over the same lines to preserve them while sped up.\n'
            '- overlay: show an on-screen caption over a span; give "text".\n'
            "- keep: protect a span from cutting, INCLUDING its silences/"
            "non-speech gaps (which are dropped by default).\n"
            '- edit: request a fine within-line text deletion/fix; describe it in "note".\n'
            "\n"
            "JSON shape:\n"
            '{"ops": [\n'
            '  {"type": "cut", "lines": [12, 18], "note": "why / where precisely"},\n'
            '  {"type": "speed", "lines": [30, 34], "factor": 2.0, "note": "..."},\n'
            '  {"type": "overlay", "lines": [5, 5], "text": "ポイント", "note": ""},\n'
            '  {"type": "keep", "lines": [40, 42], "note": "..."},\n'
            '  {"type": "edit", "lines": [7, 7], "note": "delete the redundant restatement"}\n'
            "]}\n"
            "\n"
            "Rules:\n"
            '- "lines" must be within the transcript range.\n'
            '- Use "note" to describe in natural language precisely WHERE in the '
            "line(s) the edit starts and ends, so a downstream editor can place "
            "it exactly.\n"
            "- Output only the JSON object, no other text."
        ),
    },
    # Pipeline "guided_edit" stage (Pass B2): a small local LLM applies the
    # director's operations one at a time, inserting <cut>/<speed>/<overlay>/
    # <keep> tags (and {{old->new}} patches for "edit" ops) into the verbatim
    # _edits.txt at the precise position.  Disabled by default (no-op).
    "guided_edit": {
        "enabled": False,
        "provider": "ollama_chat",
        "api_base": "",
        "model": "qwen3.5:4b",
        "api_key": "",
        "temperature": 0.1,
        "thinking": False,
        "timeout": 60,
        "context_lines": 1,
        "max_retries": 2,
        "retry_temp_step": 0.2,
        "retry_temp_cap": 0.8,
        "prompt": (
            "You apply ONE editing instruction to Japanese subtitle lines.\n"
            "You are given numbered lines and an instruction. Insert the "
            "requested marker into the line text at the precise position "
            "described, and return the lines unchanged otherwise.\n"
            "\n"
            "Markers:\n"
            "- Cut a span:    wrap it in <cut>...</cut>\n"
            '- Speed up:      wrap it in <speed factor="N.N">...</speed>\n'
            '- Overlay text:  wrap it in <overlay text="...">...</overlay>\n'
            "- Keep/protect:  wrap it in <keep>...</keep>\n"
            "- Delete words within a line: {{old->}} (old copied verbatim)\n"
            "- Fix words within a line:    {{old->new}}\n"
            "\n"
            "Rules:\n"
            "- Copy each line fully with its number. Change ONLY by inserting "
            "markers or {{old->new}}; never rephrase or reorder the original text.\n"
            "- For a span across multiple lines, open the tag on the first line "
            "and close it on the last line.\n"
            "- Output the same numbered lines, nothing else."
        ),
    },
    "audio_silence": {
        "enabled": True,
        "noise": -30.0,
        "min_silence": 0.8,
    },
    "intervals": {
        "silence_threshold": 1.5,
        "min_keep": 1.0,
        "keep_pre_margin": 1.0,
        "keep_post_margin": 1.0,
        "caption": {
            "max_bunsetu": 12,
            "max_duration": 4.0,
            "min_bunsetu": 3,
            "min_duration": 1.5,
            "silence_flush": 1.5,
            "bunsetu_separator": " ",
            "pre_margin": 0.0,
            "post_margin": 0.0,
        },
        "bunsetu": {
            "char_eps": 0.02,
            "silence_max_word_span": 0.6,
        },
    },
    "blender": {
        "default_fps": 30.0,
        "use_proxy": True,
        "proxy_size": 100,
        "caption_style": {
            "font_size": 50,
            "alignment_x": "CENTER",
            "anchor_y": "BOTTOM",
            "location_x": 0.5,
            "location_y": 0.05,
        },
        "overlay_style": {
            "anchor_y": "TOP",
            "location_y": 0.95,
        },
        "speed_mark": {
            "enabled": True,
            "template": "x{factor}",
            "font_size": 35,
            "alignment_x": "RIGHT",
            "anchor_y": "TOP",
            "location_x": 0.95,
            "location_y": 0.95,
        },
    },
    "pipeline": {
        "input_videos_dir": "src_video",
        "output_dir": "output",
        "from_stage": 1,
    },
}


def load_config(path: Path | None) -> dict:
    """Load a YAML config file. Returns ``{}`` when *path* is ``None``."""
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*. *override* wins."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def get_effective_config(
    config_path: Path | None,
    cli_overrides: dict | None = None,
) -> dict:
    """Return the fully resolved config: DEFAULTS ← config file ← CLI overrides.

    Only non-``None`` leaves in *cli_overrides* are applied so that argparse
    defaults (set to ``None``) do not mask config-file values.
    """
    file_cfg = load_config(config_path)
    merged = deep_merge(DEFAULTS, file_cfg)
    if cli_overrides:
        merged = deep_merge(merged, cli_overrides)
    if config_path is not None:
        logging.info("Config loaded from %s", config_path)
    return merged
