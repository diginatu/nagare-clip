"""transcription stage: Haas ASR request building and WhisperX-shaped output.

The ``transcribe/`` worker runs Haas on the chunks packed from the audio_silence
VAD regions; this module builds its request and shapes its tokens into
``{stem}.json`` / ``{stem}.txt``.
"""

from __future__ import annotations

import json
from pathlib import Path

from nagare_clip.audio_silence.vad import read_vad
from nagare_clip.transcription.chunks import pack_regions
from nagare_clip.transcription.whisperx_json import build_whisperx_json, transcript_text


def asr_request(sources: list[tuple[Path, Path, Path]], cfg: dict) -> dict:
    """The ``asr`` worker request for ``(media, vad_json, asr_out)`` triples."""
    t = cfg["transcription"]
    return {
        "model": t["model"],
        "batch_size": t["batch_size"],
        "max_chunk_seconds": t["max_chunk_seconds"],
        "sources": [
            {
                "path": str(media),
                "out": str(out),
                "chunks": [
                    [a, b] for a, b in pack_regions(read_vad(vad)[1], t["max_chunk_seconds"])
                ],
            }
            for media, vad, out in sources
        ],
    }


def finish_transcription(asr_out: Path, vad_json: Path, out_json: Path, out_txt: Path) -> None:
    """Shape the worker's tokens into the WhisperX-compatible JSON and the text."""
    tokens = json.loads(Path(asr_out).read_text(encoding="utf-8"))["tokens"]
    _, speech = read_vad(vad_json)
    data = build_whisperx_json(tokens, speech)
    Path(out_json).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(out_txt).write_text(transcript_text(data), encoding="utf-8")
