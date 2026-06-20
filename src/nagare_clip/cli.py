"""Stage 3 CLI entry point: apply patches, sync JSON, compute keep intervals."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import List, Tuple

import spacy

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.config import get_effective_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.stage3.bunsetu import build_bunsetu_times
from nagare_clip.stage3.captions import apply_caption_margins, collect_captions
from nagare_clip.stage3.intervals import (
    apply_margins,
    enforce_min_keep_duration,
    ensure_keep_covers_captions,
    invert_intervals,
    merge_intervals,
    subtract_intervals,
)
from nagare_clip.stage3.io import infer_source_file
from nagare_clip.stage3.speech import build_speech_spans, get_duration_sec
from nagare_clip.stage3.sync_json import (
    extract_keep_ranges,
    extract_overlay_ranges,
    extract_speed_ranges,
    sync_text_to_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply text patches, sync JSON, and compute keep intervals."
    )
    parser.add_argument(
        "--edits-txt",
        required=True,
        dest="edits_txt",
        help="Stage 2 _edits.txt path (may contain {{old->new}} markers)",
    )
    parser.add_argument(
        "--json", required=True, dest="json_path", help="WhisperX JSON path"
    )
    parser.add_argument(
        "--cuts-txt",
        dest="cuts_txt",
        default=None,
        help="Stage 2 audio-silence cut list; ranges are unioned into excludes",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--silence_threshold",
        type=float,
        default=None,
        help="Silence gap threshold in seconds",
    )
    parser.add_argument(
        "--min_keep",
        type=float,
        default=None,
        help="Minimum keep interval length in seconds",
    )
    parser.add_argument(
        "--keep_pre_margin",
        type=float,
        default=None,
        help="Seconds to extend each keep interval before its start (default: 1.0)",
    )
    parser.add_argument(
        "--keep_post_margin",
        type=float,
        default=None,
        help="Seconds to extend each keep interval after its end (default: 1.0)",
    )
    parser.add_argument(
        "--caption_max_bunsetu",
        type=int,
        default=None,
        help="Maximum bunsetsu units per caption chunk (default: 12)",
    )
    parser.add_argument(
        "--caption_max_duration",
        type=float,
        default=None,
        help="Maximum seconds per caption chunk (default: 4.0)",
    )
    parser.add_argument(
        "--caption_min_bunsetu",
        type=int,
        default=None,
        help="Minimum bunsetsu units before a chunk can be flushed (default: 3)",
    )
    parser.add_argument(
        "--caption_min_duration",
        type=float,
        default=None,
        help="Minimum seconds of speech before flushing a caption chunk (default: 1.5)",
    )
    parser.add_argument(
        "--caption_silence_flush",
        type=float,
        default=None,
        help="Silence duration that forces flushing the current caption chunk (default: 1.5)",
    )
    parser.add_argument(
        "--caption_bunsetu_separator",
        type=str,
        default=None,
        help="Separator inserted between bunsetsu units in caption text; use empty string to disable (default: ' ')",
    )
    parser.add_argument(
        "--caption_pre_margin",
        type=float,
        default=None,
        help="Seconds to extend each caption before its start (default: 0.0)",
    )
    parser.add_argument(
        "--caption_post_margin",
        type=float,
        default=None,
        help="Seconds to extend each caption after its end (default: 0.0)",
    )
    parser.add_argument(
        "--output", required=True, dest="output_path", help="Output JSON path"
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Path to log file; appends to existing file (default: console only)",
    )
    return parser.parse_args()


def _build_cli_overrides(args: argparse.Namespace) -> dict:
    """Build a nested override dict from explicitly-provided CLI arguments."""
    overrides: dict = {}

    # Stage 3 flat keys
    stage3_map = {
        "silence_threshold": "silence_threshold",
        "min_keep": "min_keep",
        "keep_pre_margin": "keep_pre_margin",
        "keep_post_margin": "keep_post_margin",
    }
    for attr, key in stage3_map.items():
        val = getattr(args, attr, None)
        if val is not None:
            overrides.setdefault("intervals", {})[key] = val

    # Stage 3 caption keys
    caption_map = {
        "caption_max_bunsetu": "max_bunsetu",
        "caption_max_duration": "max_duration",
        "caption_min_bunsetu": "min_bunsetu",
        "caption_min_duration": "min_duration",
        "caption_silence_flush": "silence_flush",
        "caption_bunsetu_separator": "bunsetu_separator",
        "caption_pre_margin": "pre_margin",
        "caption_post_margin": "post_margin",
    }
    for attr, key in caption_map.items():
        val = getattr(args, attr, None)
        if val is not None:
            overrides.setdefault("intervals", {}).setdefault("caption", {})[key] = val

    # General
    if args.log_level is not None:
        overrides.setdefault("general", {})["log_level"] = args.log_level

    return overrides


def main() -> None:
    args = parse_args()

    config_path = Path(args.config_path) if args.config_path else None
    cli_overrides = _build_cli_overrides(args)
    cfg = get_effective_config(config_path, cli_overrides)

    s3 = cfg["intervals"]
    cap = s3["caption"]
    bun = s3["bunsetu"]

    setup_logging(
        cfg["general"]["log_level"],
        args.log_file or cfg["general"]["log_file"] or None,
    )

    edits_txt = Path(args.edits_txt)
    json_path = Path(args.json_path)
    output_path = Path(args.output_path)

    # --- Sync edit lines → JSON (applies {{old->new}} patches internally) ---
    edit_lines = edits_txt.read_text(encoding="utf-8").splitlines()
    logging.info("Stage 3: syncing edits from %s", edits_txt.name)

    with json_path.open("r", encoding="utf-8") as f:
        whisperx_data = json.load(f)

    whisperx_data = sync_text_to_json(whisperx_data, edit_lines)
    force_keep_ranges = extract_keep_ranges(edit_lines, whisperx_data)
    speed_ranges = extract_speed_ranges(edit_lines, whisperx_data)
    overlay_ranges = extract_overlay_ranges(edit_lines, whisperx_data)
    if force_keep_ranges:
        logging.info(
            "Force-keep ranges from <keep>: %d", len(force_keep_ranges)
        )
    if speed_ranges:
        logging.info("Speed ranges from <speed>: %d", len(speed_ranges))
    if overlay_ranges:
        logging.info("Overlay ranges from <overlay>: %d", len(overlay_ranges))

    logging.info(
        "Loaded %d segment(s) from %s",
        len(whisperx_data.get("segments", [])),
        json_path.name,
    )

    nlp = spacy.load("ja_ginza")
    all_bunsetu_times = build_bunsetu_times(
        whisperx_data,
        nlp,
        char_eps=bun["char_eps"],
        silence_max_word_span=bun["silence_max_word_span"],
    )

    speech_spans = build_speech_spans(whisperx_data)
    duration_sec = get_duration_sec(whisperx_data, all_bunsetu_times)
    logging.info("Duration: %.1fs, bunsetsu: %d", duration_sec, len(all_bunsetu_times))

    excludes: List[Tuple[float, float]] = []

    silence_excludes = 0
    for idx in range(len(speech_spans) - 1):
        current_end = speech_spans[idx][1]
        next_start = speech_spans[idx + 1][0]
        gap = next_start - current_end
        if gap > s3["silence_threshold"]:
            logging.debug(
                "Silence gap: %.3f-%.3f (%.3fs)", current_end, next_start, gap
            )
            excludes.append((current_end, next_start))
            silence_excludes += 1

    if speech_spans and speech_spans[0][0] > s3["silence_threshold"]:
        logging.debug(
            "Silence gap: 0.000-%.3f (%.3fs) [leading]",
            speech_spans[0][0],
            speech_spans[0][0],
        )
        excludes.append((0.0, speech_spans[0][0]))
        silence_excludes += 1

    if speech_spans and (duration_sec - speech_spans[-1][1]) > s3["silence_threshold"]:
        logging.debug(
            "Silence gap: %.3f-%.3f (%.3fs) [trailing]",
            speech_spans[-1][1],
            duration_sec,
            duration_sec - speech_spans[-1][1],
        )
        excludes.append((speech_spans[-1][1], duration_sec))
        silence_excludes += 1

    logging.info("Silence excluded: %d interval(s)", silence_excludes)

    if args.cuts_txt:
        cut_ranges = read_cuts(Path(args.cuts_txt))
        excludes.extend(cut_ranges)
        logging.info(
            "Audio-silence cuts unioned: %d range(s) from %s",
            len(cut_ranges),
            Path(args.cuts_txt).name,
        )

    bounded_excludes = [
        (max(0.0, start), min(duration_sec, end))
        for start, end in excludes
        if end > start
    ]
    # Only <keep> force-preserves audio. <speed> no longer carves silence out of
    # the excludes — it is purely a playback-speed annotation (its span is still
    # emitted verbatim in speed_ranges below). To keep AND speed a region, nest
    # <speed> inside <keep>.
    all_force_keep: List[Tuple[float, float]] = list(force_keep_ranges)
    if all_force_keep:
        bounded_excludes = subtract_intervals(bounded_excludes, all_force_keep)
    merged_excludes = merge_intervals(bounded_excludes)
    keep_intervals = invert_intervals(merged_excludes, duration_sec)
    filtered_keep = [
        {"start": round(start, 3), "end": round(end, 3)}
        for start, end in keep_intervals
        if (end - start) >= s3["min_keep"]
    ]
    logging.info("Keep intervals before margins: %d", len(filtered_keep))

    keep_intervals_dicts = apply_margins(
        filtered_keep,
        pre_margin=s3["keep_pre_margin"],
        post_margin=s3["keep_post_margin"],
        duration_sec=duration_sec,
    )
    logging.info(
        "After keep margins (pre=%.2fs post=%.2fs): %d interval(s)",
        s3["keep_pre_margin"],
        s3["keep_post_margin"],
        len(keep_intervals_dicts),
    )

    captions = collect_captions(
        all_bunsetu_times,
        keep_intervals_dicts,
        max_duration=cap["max_duration"],
        max_bunsetu=cap["max_bunsetu"],
        min_bunsetu=cap["min_bunsetu"],
        min_duration=cap["min_duration"],
        silence_flush=cap["silence_flush"],
        duration_sec=duration_sec,
        bunsetu_separator=cap["bunsetu_separator"],
    )
    logging.info("Captions: %d chunk(s)", len(captions))

    if cap["pre_margin"] > 0.0 or cap["post_margin"] > 0.0:
        captions = apply_caption_margins(
            captions,
            pre_margin=cap["pre_margin"],
            post_margin=cap["post_margin"],
            duration_sec=duration_sec,
        )
        logging.info(
            "After caption margins (pre=%.2fs post=%.2fs): %d caption(s)",
            cap["pre_margin"],
            cap["post_margin"],
            len(captions),
        )

    keep_intervals_dicts = ensure_keep_covers_captions(
        keep_intervals_dicts,
        captions,
        duration_sec,
    )
    logging.info("After caption expansion: %d interval(s)", len(keep_intervals_dicts))

    keep_intervals_dicts = enforce_min_keep_duration(
        keep_intervals_dicts,
        s3["min_keep"],
        duration_sec,
    )
    logging.info(
        "After min_keep enforcement: %d interval(s)", len(keep_intervals_dicts)
    )

    output_data = {
        "source_file": infer_source_file(whisperx_data, json_path),
        "duration_sec": round(duration_sec, 3),
        "keep_intervals": keep_intervals_dicts,
        "captions": captions,
    }
    if speed_ranges:
        # Speed ranges are emitted as an independent top-level array (like
        # overlays); Stage 5 splits keep intervals at these boundaries so a
        # speed range may cover an arbitrary sub-range of a keep interval.
        output_data["speed_ranges"] = [
            {"start": round(s, 3), "end": round(e, 3), "factor": f}
            for s, e, f in speed_ranges
        ]
    if overlay_ranges:
        output_data["overlays"] = [
            {"start": round(s, 3), "end": round(e, 3), "text": t}
            for s, e, t in overlay_ranges
        ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Writing output to %s", output_path)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
