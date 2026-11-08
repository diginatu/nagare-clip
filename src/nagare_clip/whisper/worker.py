"""faster-whisper worker. Runs INSIDE the whisperx image, script piped on stdin.

Standalone on purpose: it never imports ``nagare_clip`` (the image does not have
it) and imports ``faster_whisper`` only inside ``main()`` so the pure helpers are
testable in the main environment.

Usage: ``python - <request.json>`` (container paths). Request::

    {"model": "large-v3", "beam_size": 5,
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


SAMPLE_RATE = 16000
MAX_WINDOW_SECONDS = 30.0


def decode_chunks(model, audio, chunks: list[tuple[float, float]], beam_size: int) -> list[str]:
    """Decode each chunk with its own ``model.transcribe`` call.

    Per-chunk calls make attribution exact. (``BatchedInferencePipeline`` with
    ``clip_timestamps`` merges neighbouring short clips into one window and
    returns one segment, so texts cannot be assigned back to chunks.)
    """
    texts: list[str] = []
    for s, e in chunks:
        if e - s > MAX_WINDOW_SECONDS:
            print(
                f"[whisper] warning: chunk {s:.1f}-{e:.1f}s is {e - s:.1f}s, longer than "
                f"{MAX_WINDOW_SECONDS:.0f}s; Whisper decodes 30 s windows, the tail may be lost",
                file=sys.stderr,
            )
        segments, _ = model.transcribe(
            audio[int(s * SAMPLE_RATE) : int(e * SAMPLE_RATE)],
            vad_filter=False,
            language="ja",
            beam_size=beam_size,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        texts.append(_WS.sub("", "".join(sg.text for sg in segments)))
    return texts


def find_model(model: str, root: str = MODEL_ROOT) -> str:
    pattern = os.path.join(root, f"models--Systran--faster-whisper-{model}", "snapshots", "*")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"no baked model found: {pattern}")
    return matches[0]


def load_model(model: str):
    from faster_whisper import WhisperModel

    return WhisperModel(find_model(model), device="cuda", compute_type="float16")


def _give_to_owner(path: str) -> None:
    """chown *path* to ``$NAGARE_OWNER`` (host ``uid:gid``; the container is root).

    Mirrors ``pipeline.external.OWNER_ENV`` -- this script never imports
    nagare_clip. Without it the output is root-owned on the host.
    """
    owner = os.environ.get("NAGARE_OWNER", "")
    try:
        uid, gid = (int(x) for x in owner.split(":"))
    except ValueError:
        return
    try:
        os.chown(path, uid, gid)
    except OSError as exc:
        print(f"whisper: could not chown {path}: {exc}", file=sys.stderr)


def main(argv: list[str]) -> None:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    with open(argv[0], encoding="utf-8") as f:
        request = json.load(f)
    sources = request["sources"]
    model = None
    decode_audio = None
    for src in sources:
        chunks = [(float(s), float(e)) for s, e in src["chunks"]]
        texts: list[str] = []
        if chunks:
            if model is None:
                from faster_whisper import decode_audio

                model = load_model(request["model"])
            audio = decode_audio(src["path"])
            texts = decode_chunks(model, audio, chunks, int(request["beam_size"]))
        out_dir = os.path.dirname(src["out"])
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(src["out"], "w", encoding="utf-8") as f:
            json.dump(
                {"chunks": [list(c) for c in chunks], "texts": texts},
                f,
                ensure_ascii=False,
            )
        _give_to_owner(src["out"])


if __name__ == "__main__":
    main(sys.argv[1:])
