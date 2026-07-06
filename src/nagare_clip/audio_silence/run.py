"""audio_silence stage: audio-silence (jump-cut) detection checkpoint.

ffmpeg ``silencedetect`` runs inside the whisperx Docker image (driven by the
pipeline orchestrator); this function consumes the captured stderr and writes
a human-editable ``{stem}_cuts.txt``. Without *raw_path* (or when
``audio_silence.enabled`` is false) it writes a header-only file so the
downstream union in the intervals stage becomes a no-op.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.audio_silence.detect import parse_silencedetect_output


def run_audio_silence(output: Path, cfg: dict, *, raw_path: Path | None = None) -> None:
    a = cfg["audio_silence"]
    if not a["enabled"] or raw_path is None:
        reason = "disabled" if not a["enabled"] else "no ffmpeg output provided"
        logging.info("audio_silence: audio-silence %s, writing empty cut list", reason)
        write_cuts(output, [])
        logging.info("audio_silence: wrote %s", output)
        return

    stderr = raw_path.read_text(encoding="utf-8")
    ranges = parse_silencedetect_output(stderr)
    write_cuts(output, ranges)
    logging.info("audio_silence: detected %d silence range(s), wrote %s", len(ranges), output)
