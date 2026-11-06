"""faster-whisper worker. Runs INSIDE the whisperx image, script piped on stdin.

Standalone on purpose: it never imports ``nagare_clip`` (the image does not have
it) and imports ``faster_whisper`` only inside ``main()`` so the pure helpers are
testable in the main environment.

Usage: ``python - <request.json>`` (container paths). Request::

    {"model": "large-v3", "batch_size": 8, "beam_size": 5,
     "sources": [{"path": "...", "out": "...", "chunks": [[s, e], ...]}]}

Each source's ``out`` gets ``{"chunks": [[s, e], ...], "texts": [str, ...]}``,
one whitespace-free text per chunk.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

MODEL_ROOT = "/.cache/huggingface/hub"
_WS = re.compile(r"\s+")


def group_by_chunk(
    chunks: list[tuple[float, float]], segments: list[tuple[float, str]]
) -> list[str]:
    """Join each segment's text onto the chunk containing its start.

    A start inside no chunk goes to the nearest one. Whitespace is removed.
    """
    parts: list[list[str]] = [[] for _ in chunks]
    if not chunks:
        return []
    for start, text in segments:
        idx = None
        for i, (s, e) in enumerate(chunks):
            if s <= start < e:
                idx = i
                break
        if idx is None:
            idx = min(
                range(len(chunks)),
                key=lambda i: (
                    0.0
                    if chunks[i][0] <= start <= chunks[i][1]
                    else min(abs(start - chunks[i][0]), abs(start - chunks[i][1]))
                ),
            )
        parts[idx].append(text)
    return [_WS.sub("", "".join(p)) for p in parts]


def find_model(model: str, root: str = MODEL_ROOT) -> str:
    pattern = os.path.join(root, f"models--Systran--faster-whisper-{model}", "snapshots", "*")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"no baked model found: {pattern}")
    return matches[0]


def load_model(model: str):
    from faster_whisper import WhisperModel

    return WhisperModel(find_model(model), device="cuda", compute_type="float16")


def main(argv: list[str]) -> None:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    with open(argv[0], encoding="utf-8") as f:
        request = json.load(f)
    sources = request["sources"]
    pipeline = None
    decode_audio = None
    for src in sources:
        chunks = [(float(s), float(e)) for s, e in src["chunks"]]
        texts: list[str] = []
        if chunks:
            if pipeline is None:
                from faster_whisper import BatchedInferencePipeline, decode_audio

                pipeline = BatchedInferencePipeline(load_model(request["model"]))
            audio = decode_audio(src["path"])
            segments, _ = pipeline.transcribe(
                audio,
                clip_timestamps=[{"start": s, "end": e} for s, e in chunks],
                batch_size=int(request["batch_size"]),
                vad_filter=False,
                language="ja",
                beam_size=int(request["beam_size"]),
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            texts = group_by_chunk(chunks, [(sg.start, sg.text) for sg in segments])
        out_dir = os.path.dirname(src["out"])
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(src["out"], "w", encoding="utf-8") as f:
            json.dump(
                {"chunks": [list(c) for c in chunks], "texts": texts},
                f,
                ensure_ascii=False,
            )


if __name__ == "__main__":
    main(sys.argv[1:])
