"""sentence_split stage: split long transcript lines at sentence boundaries.

A transcript line (one ``{stem}.json`` segment / ``.txt`` line) is a speech span
between two audio_silence cuts. A line longer than
``sentence_split.max_line_seconds`` is sent, alone, to an LLM that splits it at
natural sentence or clause boundaries; every other line is kept as it is (the
same dict). Lines are never merged. When ``sentence_split.enabled`` is false the
transcription files are copied through byte-identically.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.sentence_split.llm import CallLLM, split_line
from nagare_clip.sentence_split.nlp import bunsetsu_units, load_nlp
from nagare_clip.sentence_split.segment import (
    char_to_word_index,
    concat_word_text,
    rebuild_segments,
)

logger = logging.getLogger(__name__)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_long_line(seg: dict[str, Any], max_line_seconds: float) -> bool:
    """True when ``seg`` has numeric times spanning more than the threshold."""
    start, end = seg.get("start"), seg.get("end")
    if not (_is_number(start) and _is_number(end)):
        return False
    return end - start > max_line_seconds


def _split_one(
    seg: dict[str, Any],
    sp_cfg: dict[str, Any],
    nlp: Any,
    *,
    recorder: Recorder,
    unit: str,
    call_llm: CallLLM | None,
) -> list[dict[str, Any]]:
    """The pieces of one long line, or ``[seg]`` when it stays whole."""
    words = seg.get("words", [])
    text = "".join(str(w.get("word", "")) for w in words)
    if not text:
        return [seg]
    bunsetsu = bunsetsu_units(text, nlp)
    kw: dict[str, Any] = {"recorder": recorder, "unit": unit}
    if call_llm is not None:
        kw["call_llm"] = call_llm
    # split_line makes no call for fewer than two bunsetsu
    ranges = split_line(bunsetsu, sp_cfg, **kw)
    if ranges is None:
        return [seg]
    pieces = rebuild_segments(words, bunsetsu, ranges, char_to_word_index(words))
    if concat_word_text(pieces) != concat_word_text([seg]):
        logger.error("sentence_split: verbatim check failed for %s; keeping the line", unit)
        return [seg]
    if len(pieces) < 2:  # a single range: the line is one sentence
        return [seg]
    return pieces


def split_long_lines(
    json_data: dict[str, Any],
    sp_cfg: dict[str, Any],
    get_nlp: Callable[[], Any],
    *,
    recorder: Recorder = NULL_RECORDER,
    stem: str = "",
    call_llm: CallLLM | None = None,
) -> dict[str, Any]:
    """Return ``json_data`` with each long line replaced by its pieces.

    One LLM call per long line (unit ``"{stem} line N"``, N 1-based), in order.
    A short or untimed line is the same dict in the output. When no line is
    split, ``json_data`` itself is returned. ``get_nlp`` is called once, at
    the first long line, so GiNZA never loads when no line is long.
    """
    max_s = float(sp_cfg["max_line_seconds"])
    segments = json_data.get("segments", [])
    new_segments: list[dict[str, Any]] = []
    split = False
    nlp: Any = None
    loaded = False
    for n, seg in enumerate(segments, 1):
        if not is_long_line(seg, max_s):
            new_segments.append(seg)
            continue
        if not loaded:
            nlp, loaded = get_nlp(), True
        pieces = _split_one(
            seg, sp_cfg, nlp, recorder=recorder, unit=f"{stem} line {n}", call_llm=call_llm
        )
        split = split or len(pieces) > 1
        new_segments.extend(pieces)
    if not split:
        return json_data
    out = dict(json_data)
    out["segments"] = new_segments
    out["word_segments"] = [w for seg in new_segments for w in seg.get("words", [])]
    return out


def _copy_through(src_json: Path, src_txt: Path, out_json: Path, out_txt: Path) -> None:
    """Copy the transcription ``.json``/``.txt`` through byte-identically."""
    shutil.copyfile(src_json, out_json)
    shutil.copyfile(src_txt, out_txt)


def run_sentence_split(
    json_in: Path,
    txt_in: Path,
    output_json: Path,
    output_txt: Path,
    cfg: dict,
    *,
    stem: str = "",
    recorder: Recorder = NULL_RECORDER,
) -> None:
    sp_cfg = cfg["sentence_split"]
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_txt.parent.mkdir(parents=True, exist_ok=True)

    if not sp_cfg.get("enabled", False):
        logging.info("sentence_split: disabled, copying %s through", stem)
        _copy_through(json_in, txt_in, output_json, output_txt)
        return

    json_data = json.loads(json_in.read_text(encoding="utf-8"))
    new_data = split_long_lines(json_data, sp_cfg, lambda: load_nlp(), recorder=recorder, stem=stem)
    if new_data is json_data:
        # nothing split: keep the transcription files byte-identical (a
        # speechless source's "" .txt must not become "\n")
        logging.info("sentence_split: %s no line split, copying through", stem)
        _copy_through(json_in, txt_in, output_json, output_txt)
        return

    output_json.write_text(
        json.dumps(new_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_txt.write_text(
        "\n".join(seg.get("text", "") for seg in new_data.get("segments", [])) + "\n",
        encoding="utf-8",
    )
    logging.info(
        "sentence_split: %s %d -> %d segments",
        stem,
        len(json_data.get("segments", [])),
        len(new_data.get("segments", [])),
    )
