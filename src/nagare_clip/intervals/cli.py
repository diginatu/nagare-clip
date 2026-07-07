"""intervals stage CLI entry point: apply patches, sync JSON, compute keep intervals."""

from __future__ import annotations

import argparse
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.intervals.run import run_intervals
from nagare_clip.logging_setup import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply text patches, sync JSON, and compute keep intervals."
    )
    parser.add_argument(
        "--edits-txt",
        required=True,
        dest="edits_txt",
        help="text_filter _edits.txt path (may contain {{old->new}} markers)",
    )
    parser.add_argument("--json", required=True, dest="json_path", help="WhisperX JSON path")
    parser.add_argument(
        "--cuts-txt",
        dest="cuts_txt",
        default=None,
        help="audio_silence cut list; ranges are unioned into excludes",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--silence_threshold",
        type=float,
        default=None,
        help="Silence gap threshold in seconds",
    )
    parser.add_argument(
        "--min_keep",
        type=float,
        default=None,
        help="Minimum keep interval length in seconds",
    )
    parser.add_argument(
        "--keep_pre_margin",
        type=float,
        default=None,
        help="Seconds to extend each keep interval before its start (default: 1.0)",
    )
    parser.add_argument(
        "--keep_post_margin",
        type=float,
        default=None,
        help="Seconds to extend each keep interval after its end (default: 1.0)",
    )
    parser.add_argument(
        "--caption_max_bunsetu",
        type=int,
        default=None,
        help="Maximum bunsetsu units per caption chunk (default: 12)",
    )
    parser.add_argument(
        "--caption_max_duration",
        type=float,
        default=None,
        help="Maximum seconds per caption chunk (default: 4.0)",
    )
    parser.add_argument(
        "--caption_min_bunsetu",
        type=int,
        default=None,
        help="Minimum bunsetsu units before a chunk can be flushed (default: 3)",
    )
    parser.add_argument(
        "--caption_min_duration",
        type=float,
        default=None,
        help="Minimum seconds of speech before flushing a caption chunk (default: 1.5)",
    )
    parser.add_argument(
        "--caption_silence_flush",
        type=float,
        default=None,
        help="Silence duration that forces flushing the current caption chunk (default: 1.5)",
    )
    parser.add_argument(
        "--caption_bunsetu_separator",
        type=str,
        default=None,
        help="Separator inserted between bunsetsu units in caption text; use empty string to disable (default: ' ')",
    )
    parser.add_argument(
        "--caption_pre_margin",
        type=float,
        default=None,
        help="Seconds to extend each caption before its start (default: 0.0)",
    )
    parser.add_argument(
        "--caption_post_margin",
        type=float,
        default=None,
        help="Seconds to extend each caption after its end (default: 0.0)",
    )
    parser.add_argument("--output", required=True, dest="output_path", help="Output JSON path")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Path to log file; appends to existing file (default: console only)",
    )
    return parser.parse_args()


def _build_cli_overrides(args: argparse.Namespace) -> dict:
    """Build a nested override dict from explicitly-provided CLI arguments."""
    overrides: dict = {}

    # intervals flat keys
    intervals_map = {
        "silence_threshold": "silence_threshold",
        "min_keep": "min_keep",
        "keep_pre_margin": "keep_pre_margin",
        "keep_post_margin": "keep_post_margin",
    }
    for attr, key in intervals_map.items():
        val = getattr(args, attr, None)
        if val is not None:
            overrides.setdefault("intervals", {})[key] = val

    # intervals caption keys
    caption_map = {
        "caption_max_bunsetu": "max_bunsetu",
        "caption_max_duration": "max_duration",
        "caption_min_bunsetu": "min_bunsetu",
        "caption_min_duration": "min_duration",
        "caption_silence_flush": "silence_flush",
        "caption_bunsetu_separator": "bunsetu_separator",
        "caption_pre_margin": "pre_margin",
        "caption_post_margin": "post_margin",
    }
    for attr, key in caption_map.items():
        val = getattr(args, attr, None)
        if val is not None:
            overrides.setdefault("intervals", {}).setdefault("caption", {})[key] = val

    # General
    if args.log_level is not None:
        overrides.setdefault("general", {})["log_level"] = args.log_level

    return overrides


def main() -> None:
    args = parse_args()

    config_path = Path(args.config_path) if args.config_path else None
    cli_overrides = _build_cli_overrides(args)
    cfg = get_effective_config(config_path, cli_overrides)

    setup_logging(
        cfg["general"]["log_level"],
        args.log_file or cfg["general"]["log_file"] or None,
    )

    run_intervals(
        Path(args.edits_txt),
        Path(args.json_path),
        Path(args.output_path),
        cfg,
        cuts_txt=Path(args.cuts_txt) if args.cuts_txt else None,
    )


if __name__ == "__main__":
    main()
