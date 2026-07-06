"""audio_silence CLI: audio-silence (jump-cut) detection checkpoint.

ffmpeg ``silencedetect`` is run inside the whisperx Docker image by
``scripts/run_pipeline.sh``; this CLI consumes the captured stderr (``--raw``)
and writes a human-editable ``{stem}_cuts.txt``. Without ``--raw`` (or when
``audio_silence.enabled`` is false) it writes a header-only file so the
downstream union in the intervals stage becomes a no-op.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nagare_clip.audio_silence.run import run_audio_silence
from nagare_clip.config import get_effective_config
from nagare_clip.logging_setup import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audio-silence (jump-cut) detection checkpoint.")
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

    run_audio_silence(
        Path(args.output_path),
        cfg,
        raw_path=Path(args.raw_path) if args.raw_path else None,
    )


if __name__ == "__main__":
    main()
