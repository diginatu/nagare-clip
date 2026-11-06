"""audio_silence stage: audio-silence (jump-cut) detection checkpoint.

Silero VAD runs in the ``transcribe/`` worker (driven by the pipeline
orchestrator, which writes ``{stem}_vad.json``); this function turns the speech
regions into a human-editable ``{stem}_cuts.txt`` (the silences between them,
with the media duration recorded as a ``# media duration:`` comment).
"""

from __future__ import annotations

import logging
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.audio_silence.vad import read_vad, silences_from_speech


def run_audio_silence(output: Path, cfg: dict, *, vad_path: Path) -> None:
    duration, speech = read_vad(vad_path)
    ranges = silences_from_speech(speech, duration, cfg["audio_silence"]["min_silence"])
    # The media's own length, recorded alongside: a source with no speech has
    # no other place the pipeline could learn where it ends.
    write_cuts(output, ranges, duration=duration)
    logging.info("audio_silence: %d silence range(s), wrote %s", len(ranges), output)
