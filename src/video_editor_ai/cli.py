"""Stage 2 CLI entry point: compute keep intervals from WhisperX JSON."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import List, Tuple

from fugashi import Tagger

from video_editor_ai.stage2.captions import collect_captions
from video_editor_ai.stage2.filler import load_filler_set, normalize_word
from video_editor_ai.stage2.intervals import (
    apply_margins,
    enforce_min_keep_duration,
    ensure_keep_covers_captions,
    invert_intervals,
    merge_intervals,
)
from video_editor_ai.stage2.io import infer_source_file
from video_editor_ai.stage2.morpheme import build_morpheme_times
from video_editor_ai.stage2.speech import build_speech_spans, get_duration_sec


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute keep intervals from WhisperX word-level JSON output."
    )
    parser.add_argument(
        "--json", required=True, dest="json_path", help="WhisperX JSON path"
    )
    parser.add_argument(
        "--config",
        required=True,
        dest="config_path",
        help="Filler words YAML path",
    )
    parser.add_argument(
        "--language", required=True, help="Language key in filler config"
    )
    parser.add_argument(
        "--silence_threshold",
        type=float,
        default=1.5,
        help="Silence gap threshold in seconds",
    )
    parser.add_argument(
        "--min_keep",
        type=float,
        default=1.0,
        help="Minimum keep interval length in seconds",
    )
    parser.add_argument(
        "--pre_margin",
        type=float,
        default=1.0,
        help="Seconds to extend each keep interval before its start (default: 1.0)",
    )
    parser.add_argument(
        "--post_margin",
        type=float,
        default=1.0,
        help="Seconds to extend each keep interval after its end (default: 1.0)",
    )
    parser.add_argument(
        "--caption_max_morphemes",
        type=int,
        default=12,
        help="Maximum morphemes per caption chunk (default: 12)",
    )
    parser.add_argument(
        "--caption_max_duration",
        type=float,
        default=4.0,
        help="Maximum seconds per caption chunk (default: 4.0)",
    )
    parser.add_argument(
        "--caption_min_morphemes",
        type=int,
        default=3,
        help="Minimum morphemes before a chunk can be flushed (default: 3)",
    )
    parser.add_argument(
        "--caption_min_duration",
        type=float,
        default=1.5,
        help="Minimum seconds of speech before flushing a caption chunk (default: 1.5)",
    )
    parser.add_argument(
        "--caption_silence_flush",
        type=float,
        default=1.5,
        help="Silence duration that forces flushing the current caption chunk (default: 1.5)",
    )
    parser.add_argument(
        "--output", required=True, dest="output_path", help="Output JSON path"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO)",
    )
    parser.add_argument(
        "--word_padding",
        type=float,
        default=0.1,
        help="Padding seconds before/after excluded filler words",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s: %(message)s")

    json_path = Path(args.json_path)
    config_path = Path(args.config_path)
    output_path = Path(args.output_path)

    with json_path.open("r", encoding="utf-8") as f:
        whisperx_data = json.load(f)

    filler_set = load_filler_set(config_path, args.language)
    logging.info(
        "Loaded %d segment(s) from %s",
        len(whisperx_data.get("segments", [])),
        json_path.name,
    )

    tagger = Tagger("-Owakati")
    all_morpheme_times = build_morpheme_times(whisperx_data, tagger)

    words = all_morpheme_times
    speech_spans = build_speech_spans(whisperx_data)
    duration_sec = get_duration_sec(whisperx_data, words)
    logging.info(
        "Duration: %.1fs, morphemes: %d", duration_sec, len(all_morpheme_times)
    )

    excludes: List[Tuple[float, float]] = []

    filler_hits = 0
    for start, end, token in words:
        normalized = normalize_word(token)
        if normalized in filler_set:
            logging.debug("Filler hit: %r [%.3f-%.3f]", token, start, end)
            excludes.append((start - args.word_padding, end + args.word_padding))
            filler_hits += 1
    logging.info(
        "Filler words excluded: %d interval(s) from %d hit(s)", filler_hits, filler_hits
    )

    silence_excludes = 0
    for idx in range(len(speech_spans) - 1):
        current_end = speech_spans[idx][1]
        next_start = speech_spans[idx + 1][0]
        gap = next_start - current_end
        if gap > args.silence_threshold:
            logging.debug(
                "Silence gap: %.3f-%.3f (%.3fs)", current_end, next_start, gap
            )
            excludes.append((current_end, next_start))
            silence_excludes += 1

    if speech_spans and speech_spans[0][0] > args.silence_threshold:
        logging.debug(
            "Silence gap: 0.000-%.3f (%.3fs) [leading]",
            speech_spans[0][0],
            speech_spans[0][0],
        )
        excludes.append((0.0, speech_spans[0][0]))
        silence_excludes += 1

    if speech_spans and (duration_sec - speech_spans[-1][1]) > args.silence_threshold:
        logging.debug(
            "Silence gap: %.3f-%.3f (%.3fs) [trailing]",
            speech_spans[-1][1],
            duration_sec,
            duration_sec - speech_spans[-1][1],
        )
        excludes.append((speech_spans[-1][1], duration_sec))
        silence_excludes += 1

    logging.info("Silence excluded: %d interval(s)", silence_excludes)

    bounded_excludes = [
        (max(0.0, start), min(duration_sec, end))
        for start, end in excludes
        if end > start
    ]
    merged_excludes = merge_intervals(bounded_excludes)
    keep_intervals = invert_intervals(merged_excludes, duration_sec)
    filtered_keep = [
        {"start": round(start, 3), "end": round(end, 3)}
        for start, end in keep_intervals
        if (end - start) >= args.min_keep
    ]
    logging.info("Keep intervals before margins: %d", len(filtered_keep))

    keep_intervals_dicts = apply_margins(
        filtered_keep,
        pre_margin=args.pre_margin,
        post_margin=args.post_margin,
        duration_sec=duration_sec,
    )
    logging.info(
        "After margins (pre=%.2fs post=%.2fs): %d interval(s)",
        args.pre_margin,
        args.post_margin,
        len(keep_intervals_dicts),
    )

    captions = collect_captions(
        all_morpheme_times,
        keep_intervals_dicts,
        max_duration=args.caption_max_duration,
        max_morphemes=args.caption_max_morphemes,
        min_morphemes=args.caption_min_morphemes,
        min_duration=args.caption_min_duration,
        silence_flush=args.caption_silence_flush,
    )
    logging.info("Captions: %d chunk(s)", len(captions))

    keep_intervals_dicts = ensure_keep_covers_captions(
        keep_intervals_dicts,
        captions,
        duration_sec,
    )
    logging.info("After caption expansion: %d interval(s)", len(keep_intervals_dicts))

    keep_intervals_dicts = enforce_min_keep_duration(
        keep_intervals_dicts,
        args.min_keep,
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

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Writing output to %s", output_path)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
