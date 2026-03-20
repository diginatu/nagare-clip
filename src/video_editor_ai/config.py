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
    },
    "stage1": {
        "compute_type": "float16",
        "batch_size": 16,
        "align_model": "",
    },
    "stage2": {
        "silence_threshold": 1.5,
        "min_keep": 1.0,
        "pre_margin": 1.0,
        "post_margin": 1.0,
        "caption": {
            "max_bunsetu": 12,
            "max_duration": 4.0,
            "min_bunsetu": 3,
            "min_duration": 1.5,
            "silence_flush": 1.5,
            "bunsetu_separator": " ",
        },
        "bunsetu": {
            "char_eps": 0.02,
            "silence_max_word_span": 0.6,
        },
    },
    "stage3": {
        "default_fps": 30.0,
        "caption_style": {
            "font_size": 50,
            "alignment_x": "CENTER",
            "anchor_y": "BOTTOM",
            "location_x": 0.5,
            "location_y": 0.05,
            "use_shadow": True,
        },
    },
    "pipeline": {
        "input_videos_dir": "src_video",
        "output_dir": "output",
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
