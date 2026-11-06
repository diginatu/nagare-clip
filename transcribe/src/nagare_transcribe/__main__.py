"""Worker CLI: ``python -m nagare_transcribe {vad,asr} REQUEST``."""

import argparse
import json
from pathlib import Path

from . import asr
from .audio import SAMPLE_RATE, decode_audio


def _write(path: str, data: dict) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run_vad(req: dict) -> None:
    from . import vad

    for src in req["sources"]:
        audio = decode_audio(Path(src["path"]))
        regions = vad.speech_regions(audio, req["params"])
        _write(
            src["out"],
            {
                "duration": round(len(audio) / SAMPLE_RATE, 3),
                "speech": [[round(a, 3), round(b, 3)] for a, b in regions],
            },
        )


def run_asr(req: dict) -> None:
    model = None
    for src in req["sources"]:
        if not src["chunks"]:
            _write(src["out"], {"chunks": [], "tokens": []})
            continue
        if model is None:
            model = asr.load_model(req["model"])
        audio = decode_audio(Path(src["path"]))
        chunks = asr.split_long_chunks(audio, src["chunks"], req["max_chunk_seconds"])
        tokens = asr.transcribe_chunks(model, audio, chunks, req["batch_size"])
        _write(
            src["out"],
            {"chunks": [[round(a, 3), round(b, 3)] for a, b in chunks], "tokens": tokens},
        )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nagare_transcribe")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("vad", "asr"):
        sub.add_parser(name).add_argument("request")
    args = p.parse_args(argv)
    req = json.loads(Path(args.request).read_text(encoding="utf-8"))
    {"vad": run_vad, "asr": run_asr}[args.cmd](req)


if __name__ == "__main__":
    main()
