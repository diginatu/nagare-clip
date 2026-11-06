"""Haas-ja ASR over pre-cut chunks of one audio array."""

import re

import numpy as np

from .audio import SAMPLE_RATE

_WS = re.compile(r"[\s▁]+")


def load_model(name: str):
    import haas_asr

    return haas_asr.load_model(name)


def split_long_chunks(audio, chunks, max_seconds, splitter=None):
    """Chunks longer than ``max_seconds`` are split at quiet points."""
    if splitter is None:
        from haas_asr import split_at_quiet_points as splitter
    out: list[tuple[float, float]] = []
    for a, b in chunks:
        if b - a <= max_seconds:
            out.append((a, b))
            continue
        lo = round(a * SAMPLE_RATE)
        hi = round(b * SAMPLE_RATE)
        for s, e in splitter(audio[lo:hi], chunk_seconds=max_seconds):
            out.append((round(a + s / SAMPLE_RATE, 3), round(a + e / SAMPLE_RATE, 3)))
    return out


def tokens_from_hypothesis(h, offset: float) -> list[dict]:
    tokens = []
    for e in h.timestamp["char"]:
        text = _WS.sub("", "".join(e["char"]))
        if not text:
            continue
        tokens.append(
            {
                "text": text,
                "start": round(e["start"] + offset, 3),
                "end": round(e["end"] + offset, 3),
            }
        )
    return tokens


def transcribe_chunks(model, audio, chunks, batch_size) -> list[dict]:
    slices = [
        np.ascontiguousarray(audio[round(a * SAMPLE_RATE) : round(b * SAMPLE_RATE)])
        for a, b in chunks
    ]
    hyps = model.transcribe(slices, batch_size=batch_size, timestamps=True, verbose=False)
    tokens: list[dict] = []
    for (a, _), h in zip(chunks, hyps):
        tokens.extend(tokens_from_hypothesis(h, a))
    return tokens
