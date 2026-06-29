"""sentence_split stage CLI (per source).

Re-segments a WhisperX ``{stem}.json`` into one-sentence-per-line segments and
writes the re-segmented ``.json`` plus a matching ``.txt`` (one segment per
line).  When ``sentence_split.enabled`` is false it copies the transcription
``.json``/``.txt`` through byte-identically, so downstream behaviour is
unchanged.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

from nagare_clip.config import get_effective_config
from nagare_clip.llm_report import NULL_RECORDER, Recorder, recorder_from_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.sentence_split.llm import split_window
from nagare_clip.sentence_split.nlp import bunsetsu_units, load_nlp
from nagare_clip.sentence_split.segment import (
    char_to_word_index,
    concat_word_text,
    iter_windows,
    rebuild_window_segments,
    window_text_and_words,
)


def resegment_json(
    json_data: Dict[str, Any],
    sp_cfg: Dict[str, Any],
    nlp: Any,
    *,
    recorder: Recorder = NULL_RECORDER,
    stem: str = "",
) -> Dict[str, Any]:
    """Return a new WhisperX data dict with re-segmented segments.

    On a verbatim-invariant violation, returns ``json_data`` unchanged.
    """
    segments = json_data.get("segments", [])
    window = int(sp_cfg.get("window_segments", 20))
    new_segments = []
    for base, win in iter_windows(segments, window):
        text, words = window_text_and_words(win)
        if not text:
            new_segments.extend(win)
            continue
        bunsetsu = bunsetsu_units(text, nlp)
        if not bunsetsu:
            new_segments.extend(win)
            continue
        ranges = split_window(
            bunsetsu, sp_cfg, recorder=recorder, unit=f"{stem}.w{base + 1}"
        )
        if ranges is None:
            new_segments.extend(win)
            continue
        char2word = char_to_word_index(words)
        new_segments.extend(
            rebuild_window_segments(words, bunsetsu, ranges, char2word)
        )

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


def _copy_through(src_json: str, src_txt: str, out_json: Path, out_txt: Path) -> None:
    """Copy the transcription ``.json``/``.txt`` through byte-identically."""
    shutil.copyfile(src_json, out_json)
    shutil.copyfile(src_txt, out_txt)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="sentence_split stage: LLM re-segmentation of a WhisperX transcript."
    )
    parser.add_argument("--json", required=True, dest="json", help="Input WhisperX JSON")
    parser.add_argument("--txt", required=True, dest="txt", help="Input transcript .txt")
    parser.add_argument("--output-json", required=True, dest="output_json")
    parser.add_argument("--output-txt", required=True, dest="output_txt")
    parser.add_argument("--stem", default="", dest="stem")
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--llm-report-dir", default=None, dest="llm_report_dir")
    parser.add_argument(
        "--llm-report-no-clear", action="store_true", dest="llm_report_no_clear"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cli_overrides: Dict[str, Any] = {}
    if args.log_level is not None:
        cli_overrides.setdefault("general", {})["log_level"] = args.log_level
    cfg = get_effective_config(
        Path(args.config_path) if args.config_path else None, cli_overrides
    )
    setup_logging(
        cfg["general"]["log_level"], args.log_file or cfg["general"]["log_file"] or None
    )
    recorder = recorder_from_config(
        "sentence_split", cfg, override_dir=args.llm_report_dir
    )
    if not args.llm_report_no_clear:
        recorder.clear()

    sp_cfg = cfg["sentence_split"]
    out_json = Path(args.output_json)
    out_txt = Path(args.output_txt)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)

    try:
        if not sp_cfg.get("enabled", False):
            logging.info("sentence_split: disabled, copying %s through", args.stem)
            _copy_through(args.json, args.txt, out_json, out_txt)
            return

        json_data = json.loads(Path(args.json).read_text(encoding="utf-8"))
        nlp = load_nlp()
        new_data = resegment_json(
            json_data, sp_cfg, nlp, recorder=recorder, stem=args.stem
        )

        if new_data is json_data:
            # verbatim violation already logged; copy through for safety
            _copy_through(args.json, args.txt, out_json, out_txt)
            return

        out_json.write_text(
            json.dumps(new_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        out_txt.write_text(
            "\n".join(seg.get("text", "") for seg in new_data["segments"]) + "\n",
            encoding="utf-8",
        )
        logging.info(
            "sentence_split: %s %d -> %d segments",
            args.stem, len(json_data.get("segments", [])), len(new_data["segments"]),
        )
    finally:
        recorder.rebuild_index()


if __name__ == "__main__":
    main()
