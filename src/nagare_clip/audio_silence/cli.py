"""Stage 2 CLI: audio-silence (jump-cut) detection checkpoint.

ffmpeg ``silencedetect`` is run inside the whisperx Docker image by
``scripts/run_pipeline.sh``; this CLI consumes the captured stderr (``--raw``)
and writes a human-editable ``{stem}_cuts.txt``. Without ``--raw`` (or when
``audio_silence.enabled`` is false) it writes a header-only file so the
downstream union in Stage 4 becomes a no-op.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.audio_silence.detect import parse_silencedetect_output
from nagare_clip.config import get_effective_config
from nagare_clip.logging_setup import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audio-silence (jump-cut) detection checkpoint."
    )
    parser.add_argument(
        "--raw",
        dest="raw_path",
        default=None,
        help="Path to captured ffmpeg silencedetect stderr (optional)",
    )
    parser.add_argument(
        "--output",
        required=True,
        dest="output_path",
        help="Output cut-list .txt path",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to YAML config file",
    )
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


def main() -> None:
    args = parse_args()

    cli_overrides: dict = {}
    if args.log_level is not None:
        cli_overrides.setdefault("general", {})["log_level"] = args.log_level

    config_path = Path(args.config_path) if args.config_path else None
    cfg = get_effective_config(config_path, cli_overrides)

    setup_logging(
        cfg["general"]["log_level"],
        args.log_file or cfg["general"]["log_file"] or None,
    )

    a = cfg["audio_silence"]
    output_path = Path(args.output_path)

    if not a["enabled"] or args.raw_path is None:
        reason = "disabled" if not a["enabled"] else "no ffmpeg output provided"
        logging.info("Stage 2: audio-silence %s, writing empty cut list", reason)
        write_cuts(output_path, [])
        logging.info("Stage 2: wrote %s", output_path)
        return

    stderr = Path(args.raw_path).read_text(encoding="utf-8")
    ranges = parse_silencedetect_output(stderr)
    write_cuts(output_path, ranges)
    logging.info(
        "Stage 2: detected %d silence range(s), wrote %s",
        len(ranges),
        output_path,
    )


if __name__ == "__main__":
    main()
