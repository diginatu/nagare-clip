"""One Whisper hint per transcript line (pure; no LLM, no faster-whisper)."""

from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


def _comparable(s: str) -> str:
    """``s`` without punctuation (Unicode category P*) and whitespace."""
    return "".join(c for c in s if not (unicodedata.category(c).startswith("P") or c.isspace()))


def _nearest_chunk(start: float, chunks: list[tuple[float, float]]) -> int:
    for i, (s, e) in enumerate(chunks):
        if s <= start < e:
            return i
    return min(
        range(len(chunks)),
        key=lambda i: (
            0.0
            if chunks[i][0] <= start <= chunks[i][1]
            else min(abs(start - chunks[i][0]), abs(start - chunks[i][1]))
        ),
    )


def _widen(spans: dict[int, list[int]], line: int, lo: int, hi: int) -> None:
    cur = spans.get(line)
    if cur is None:
        spans[line] = [lo, hi]
    else:
        cur[0] = min(cur[0], lo)
        cur[1] = max(cur[1], hi)


def line_hints(
    segments: list[dict],
    chunks: list[tuple[float, float]],
    texts: list[str],
) -> list[str | None]:
    """The Whisper text covering each line, or ``None`` when it equals the line ignoring punctuation and whitespace.

    Each character joins the chunk containing its ``start`` (nearest if none).
    Per chunk the Haas characters are aligned with the Whisper text; a line's
    hint is the Whisper span over its characters, an insertion going to the
    earlier line. A line spanning chunks concatenates its spans in order.
    """
    n = len(segments)
    if not chunks or len(texts) != len(chunks):
        return [None] * n

    # chunk -> [(line, char)] in order
    per_chunk: list[list[tuple[int, str]]] = [[] for _ in chunks]
    for li, segment in enumerate(segments):
        prev = segment.get("start")
        for w in segment.get("words") or []:
            start = w.get("start")
            if start is None:
                start = prev if prev is not None else 0.0
            start = float(start)
            prev = start
            for ch in _WS.sub("", str(w.get("word", ""))):
                per_chunk[_nearest_chunk(start, chunks)].append((li, ch))

    pieces: list[list[str]] = [[] for _ in range(n)]
    for ci, chars in enumerate(per_chunk):
        if not chars:
            continue
        haas = "".join(c for _, c in chars)
        whisper = texts[ci]
        spans: dict[int, list[int]] = {}  # line -> [lo, hi) in whisper

        sm = difflib.SequenceMatcher(None, haas, whisper, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "insert":
                line = chars[i1 - 1][0] if i1 > 0 else chars[0][0]
                _widen(spans, line, j1, j2)
                continue
            m, k = j2 - j1, i2 - i1
            for off in range(k):
                lo = j1 + (off * m) // k
                hi = j1 + ((off + 1) * m) // k if tag != "delete" else j1
                _widen(spans, chars[i1 + off][0], lo, hi)
        for line, (lo, hi) in spans.items():
            pieces[line].append(whisper[lo:hi])

    out: list[str | None] = []
    for li, segment in enumerate(segments):
        text = _WS.sub("", segment.get("text", ""))
        if not segment.get("words"):
            out.append(None)
            continue
        hint = "".join(pieces[li])
        out.append(
            None
            if _comparable(hint) == _comparable(text) and (hint or not _comparable(text))
            else hint
        )
    return out


def load_line_hints(segments_json: Path, whisper_json: Path) -> list[str | None] | None:
    """Hints for one source, or ``None`` (with a warning) when unusable."""
    try:
        segments = json.loads(Path(segments_json).read_text(encoding="utf-8"))["segments"]
        data = json.loads(Path(whisper_json).read_text(encoding="utf-8"))
        chunks = [(float(s), float(e)) for s, e in data["chunks"]]
        texts = [str(t) for t in data["texts"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("whisper hints unavailable (%s / %s): %s", segments_json, whisper_json, exc)
        return None
    if len(chunks) != len(texts):
        logger.warning("whisper hints unavailable: %d chunks vs %d texts", len(chunks), len(texts))
        return None
    try:
        return line_hints(segments, chunks, texts)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        logger.warning("whisper hints unavailable (malformed segments %s): %s", segments_json, exc)
        return None
