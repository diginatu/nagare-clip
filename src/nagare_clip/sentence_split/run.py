"""sentence_split stage: LLM re-segmentation of a WhisperX transcript.

Re-segments a WhisperX ``{stem}.json`` into one-sentence-per-line segments and
writes the re-segmented ``.json`` plus a matching ``.txt``. When
``sentence_split.enabled`` is false it copies the transcription files through
byte-identically, so downstream behaviour is unchanged.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.sentence_split.llm import split_window
from nagare_clip.sentence_split.nlp import bunsetsu_units, load_nlp
from nagare_clip.sentence_split.segment import (
    char_to_word_index,
    concat_word_text,
    iter_windows,
    rebuild_window_segments,
    segment_from_words,
    split_segment_at_silences,
    window_text_and_words,
)


def resegment_json(
    json_data: dict[str, Any],
    sp_cfg: dict[str, Any],
    nlp: Any,
    *,
    recorder: Recorder = NULL_RECORDER,
    stem: str = "",
    silences: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Return a new WhisperX data dict with re-segmented segments.

    ``silences`` are already-qualifying ``(start, end)`` spans (threshold
    filtering happens in :func:`run_sentence_split`); every emitted segment is
    deterministically split at them, so no output segment spans a silence even
    when the LLM groups across one or the window degrades. Empty/omitted →
    behaviour is byte-identical to before.

    On a verbatim-invariant violation, returns ``json_data`` unchanged.
    """
    forced = silences or []

    def emit(segs: list[dict[str, Any]]) -> None:
        for s in segs:
            new_segments.extend(split_segment_at_silences(s, forced))

    segments = json_data.get("segments", [])
    window = int(sp_cfg.get("window_segments", 20))
    new_segments: list[dict[str, Any]] = []
    # The trailing (possibly incomplete) sentence of each window is carried into
    # the next window so a sentence straddling a window boundary is re-grouped
    # with its continuation rather than forced to split at the seam.
    carry_words: list = []
    windows = list(iter_windows(segments, window))
    for idx, (base, win) in enumerate(windows):
        is_last = idx == len(windows) - 1
        _, win_words = window_text_and_words(win)
        carried_in = carry_words
        words = carried_in + win_words
        text = "".join(str(w.get("word", "")) for w in words)
        carry_words = []
        if not text:
            emit(win)
            continue
        bunsetsu = bunsetsu_units(text, nlp)
        ranges = (
            split_window(bunsetsu, sp_cfg, recorder=recorder, unit=f"{stem}.w{base + 1}")
            if bunsetsu
            else None
        )
        if ranges is None:
            # Degrade: finalize anything carried in, then keep the window's
            # original segments so the fallback stays local and lossless.
            if carried_in:
                emit([segment_from_words(carried_in)])
            emit(win)
            continue
        char2word = char_to_word_index(words)
        rebuilt = rebuild_window_segments(words, bunsetsu, ranges, char2word)
        if not is_last and len(rebuilt) > 1:
            # Hold back the trailing sentence for the next window; emit the rest.
            carry_words = rebuilt[-1].get("words", [])
            emit(rebuilt[:-1])
        else:
            # Single-sentence window (or the last window): accept as-is.
            emit(rebuilt)

    if concat_word_text(new_segments) != concat_word_text(segments):
        logging.error(
            "sentence_split: verbatim invariant violated for %s; keeping original",
            stem,
        )
        return json_data

    out = dict(json_data)
    out["segments"] = new_segments
    out["word_segments"] = [w for seg in new_segments for w in seg.get("words", [])]
    return out


def _copy_through(src_json: Path, src_txt: Path, out_json: Path, out_txt: Path) -> None:
    """Copy the transcription ``.json``/``.txt`` through byte-identically."""
    shutil.copyfile(src_json, out_json)
    shutil.copyfile(src_txt, out_txt)


def _forced_silences(cuts_txt: Path | None, sp_cfg: dict[str, Any]) -> list[tuple[float, float]]:
    """Cut spans that qualify to force a sentence split.

    Reads the human-editable audio_silence cut list and keeps only spans at
    least ``force_split_min_silence`` seconds long. Disabled / missing file /
    nothing qualifying → ``[]`` (no forced boundaries).
    """
    if not cuts_txt or not sp_cfg.get("force_split", True):
        return []
    threshold = float(sp_cfg.get("force_split_min_silence", 3.0))
    return [(s, e) for s, e in read_cuts(cuts_txt) if e - s >= threshold]


def run_sentence_split(
    json_in: Path,
    txt_in: Path,
    output_json: Path,
    output_txt: Path,
    cfg: dict,
    *,
    stem: str = "",
    recorder: Recorder = NULL_RECORDER,
    cuts_txt: Path | None = None,
) -> None:
    sp_cfg = cfg["sentence_split"]
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_txt.parent.mkdir(parents=True, exist_ok=True)

    if not sp_cfg.get("enabled", False):
        logging.info("sentence_split: disabled, copying %s through", stem)
        _copy_through(json_in, txt_in, output_json, output_txt)
        return

    silences = _forced_silences(cuts_txt, sp_cfg)
    json_data = json.loads(json_in.read_text(encoding="utf-8"))
    nlp = load_nlp()
    new_data = resegment_json(
        json_data, sp_cfg, nlp, recorder=recorder, stem=stem, silences=silences
    )

    if new_data is json_data:
        # verbatim violation already logged; copy through for safety
        _copy_through(json_in, txt_in, output_json, output_txt)
        return

    output_json.write_text(
        json.dumps(new_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_txt.write_text(
        "\n".join(seg.get("text", "") for seg in new_data["segments"]) + "\n",
        encoding="utf-8",
    )
    logging.info(
        "sentence_split: %s %d -> %d segments",
        stem,
        len(json_data.get("segments", [])),
        len(new_data["segments"]),
    )
