"""Haas-ja ASR over pre-cut chunks of one audio array."""

import re
import sys

import numpy as np

from .audio import SAMPLE_RATE

_WS = re.compile(r"[\s▁]+")
_BYTE_PIECE = re.compile(r"^<0x([0-9A-Fa-f]{2})>$")


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


def tokens_from_hypothesis(h, offset: float, pieces: list[str] | None = None) -> list[dict]:
    """Tokens of one hypothesis; ``pieces`` (tokenizer pieces aligned 1:1 with the
    timestamp entries) lets byte-fallback runs be decoded into characters."""
    entries = h.timestamp["char"]
    if pieces is not None and len(pieces) != len(entries):
        print(
            f"warning: {len(pieces)} tokenizer pieces for {len(entries)} timestamp "
            "entries; byte-fallback tokens not decoded",
            file=sys.stderr,
        )
        pieces = None
    tokens = []
    i = 0
    while i < len(entries):
        if pieces is not None and _BYTE_PIECE.match(pieces[i]):
            j = i
            while j < len(entries) and _BYTE_PIECE.match(pieces[j]):
                j += 1
            raw = bytes(int(_BYTE_PIECE.match(p).group(1), 16) for p in pieces[i:j])
            tokens.append(
                {
                    "text": raw.decode("utf-8", errors="replace"),
                    "start": round(entries[i]["start"] + offset, 3),
                    "end": round(entries[j - 1]["end"] + offset, 3),
                }
            )
            i = j
            continue
        e = entries[i]
        i += 1
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


def _pieces_of(model, h) -> list[str] | None:
    ids = getattr(h, "y_sequence", None)
    ids_to_tokens = getattr(getattr(model, "tokenizer", None), "ids_to_tokens", None)
    if ids is None or ids_to_tokens is None:
        return None
    return list(ids_to_tokens([int(i) for i in ids]))


def transcribe_chunks(model, audio, chunks, batch_size) -> list[dict]:
    slices = [
        np.ascontiguousarray(audio[round(a * SAMPLE_RATE) : round(b * SAMPLE_RATE)])
        for a, b in chunks
    ]
    hyps = model.transcribe(slices, batch_size=batch_size, timestamps=True, verbose=False)
    tokens: list[dict] = []
    for (a, _), h in zip(chunks, hyps, strict=True):
        tokens.extend(tokens_from_hypothesis(h, a, _pieces_of(model, h)))
    return tokens
