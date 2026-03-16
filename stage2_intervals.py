#!/usr/bin/env python3

import argparse
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import yaml
from fugashi import Tagger


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
        "--word_padding",
        type=float,
        default=0.1,
        help="Padding seconds before/after excluded filler words",
    )
    return parser.parse_args()


def normalize_word(text: str) -> str:
    lowered = text.lower().strip()
    cleaned_chars = []
    for ch in lowered:
        category = unicodedata.category(ch)
        if category.startswith("L") or category.startswith("N") or ch.isspace():
            cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars)
    return re.sub(r"\s+", " ", cleaned).strip()


def load_filler_set(config_path: Path, language: str) -> set[str]:
    with config_path.open("r", encoding="utf-8") as f:
        config: Dict[str, Sequence[str]] = yaml.safe_load(f) or {}

    if language not in config:
        available = ", ".join(sorted(config.keys())) or "(none)"
        raise ValueError(
            f"Language '{language}' not found in {config_path}. Available: {available}"
        )

    return {normalize_word(w) for w in config[language] if normalize_word(w)}


_CHAR_EPS = 0.02
_SILENCE_MAX_WORD_SPAN = 0.6


def build_morpheme_times(
    whisperx_data: dict,
    tagger: "Tagger",
) -> List[Tuple[float, float, str]]:
    """
    Returns a flat list of (start, end, surface) for every morpheme
    across all segments, sorted by start time.

    end = min(last_char_start + _CHAR_EPS, next_morpheme_start)
    so that gap = next.start - this.end reflects real silence only.
    """
    all_morphemes: List[Tuple[float, float, str]] = []

    for segment in whisperx_data.get("segments", []):
        seg_text = segment.get("text", "").strip()
        char_entries = segment.get("words", [])
        if not seg_text or not char_entries:
            continue

        # Build char_starts: one start time per character of seg_text,
        # inheriting the last valid start for entries with missing start times.
        char_starts: List[float] = []
        last_valid = float(char_entries[0].get("start") or 0.0)
        for entry in char_entries:
            s = entry.get("start")
            if s is not None:
                last_valid = float(s)
            char_starts.append(last_valid)
        while len(char_starts) < len(seg_text):
            char_starts.append(char_starts[-1] if char_starts else 0.0)
        char_starts = char_starts[: len(seg_text)]

        # Morphological analysis
        morphemes: List[str] = [w.surface for w in tagger(seg_text)]

        # Map each morpheme to (start, tentative_end, surface).
        # tentative_end = last_char_start + eps; will be clamped below.
        seg_morphemes: List[Tuple[float, float, str]] = []
        char_cursor = 0
        for morpheme in morphemes:
            m_len = len(morpheme)
            start_idx = min(char_cursor, len(char_starts) - 1)
            last_idx = min(char_cursor + m_len - 1, len(char_starts) - 1)
            m_start = char_starts[start_idx]
            m_end = char_starts[last_idx] + _CHAR_EPS
            seg_morphemes.append((m_start, m_end, morpheme))
            char_cursor += m_len

        # Apply min(tentative_end, next_morpheme_start) within segment
        for i in range(len(seg_morphemes) - 1):
            m_start, m_end, surface = seg_morphemes[i]
            next_start = seg_morphemes[i + 1][0]
            seg_morphemes[i] = (m_start, min(m_end, next_start), surface)

        all_morphemes.extend(seg_morphemes)

    all_morphemes.sort(key=lambda x: x[0])
    return all_morphemes


def flatten_words(whisperx_data: dict) -> List[Tuple[float, float, str]]:
    from fugashi import Tagger as _Tagger

    tagger = _Tagger("-Owakati")
    return build_morpheme_times(whisperx_data, tagger)


def build_speech_spans(whisperx_data: dict) -> List[Tuple[float, float]]:
    """Build speech spans from WhisperX word timings for silence detection."""
    spans: List[Tuple[float, float]] = []

    for segment in whisperx_data.get("segments", []):
        raw_entries = segment.get("words", [])
        entries = [e for e in raw_entries if e.get("start") is not None]
        for idx, entry in enumerate(entries):
            start_raw = entry.get("start")
            end_raw = entry.get("end")

            if start_raw is None:
                continue
            start = float(start_raw)

            next_start = None
            if idx + 1 < len(entries):
                next_start = float(entries[idx + 1].get("start"))

            if end_raw is None:
                end = start + _SILENCE_MAX_WORD_SPAN
            else:
                end = float(end_raw)

            end = min(end, start + _SILENCE_MAX_WORD_SPAN)
            if next_start is not None:
                end = min(end, next_start)

            if end <= start:
                end = start + _CHAR_EPS

            spans.append((start, end))

    spans.sort(key=lambda x: x[0])
    return spans


def get_duration_sec(
    whisperx_data: dict, words: Sequence[Tuple[float, float, str]]
) -> float:
    max_end = 0.0
    if words:
        max_end = max(max_end, max(end for _, end, _ in words))

    for segment in whisperx_data.get("segments", []):
        end = segment.get("end")
        if end is not None:
            max_end = max(max_end, float(end))

    if isinstance(whisperx_data.get("duration"), (int, float)):
        max_end = max(max_end, float(whisperx_data["duration"]))

    return max_end


def merge_intervals(
    intervals: Iterable[Tuple[float, float]], epsilon: float = 1e-6
) -> List[List[float]]:
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    if not sorted_intervals:
        return []

    merged: List[List[float]] = [[sorted_intervals[0][0], sorted_intervals[0][1]]]
    for start, end in sorted_intervals[1:]:
        last = merged[-1]
        if start <= last[1] + epsilon:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return merged


def invert_intervals(
    excludes: Sequence[Sequence[float]], duration_sec: float
) -> List[List[float]]:
    keeps: List[List[float]] = []
    cursor = 0.0
    for start, end in excludes:
        start_f = max(0.0, min(float(start), duration_sec))
        end_f = max(0.0, min(float(end), duration_sec))
        if start_f > cursor:
            keeps.append([cursor, start_f])
        cursor = max(cursor, end_f)
    if cursor < duration_sec:
        keeps.append([cursor, duration_sec])
    return keeps


def infer_source_file(whisperx_data: dict, json_path: Path) -> str:
    for key in ("source_file", "source", "audio", "audio_path", "file", "input_file"):
        value = whisperx_data.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).name
    return json_path.stem


def collect_captions(
    morpheme_times: List[Tuple[float, float, str]],
    keep_intervals: List[dict],
    max_duration: float = 4.0,
    max_morphemes: int = 12,
    min_morphemes: int = 3,
    min_duration: float = 1.5,
    silence_flush: float = 1.5,
) -> List[dict]:
    del min_duration

    keep_ranges = [
        (float(iv["start"]), float(iv["end"]))
        for iv in keep_intervals
        if float(iv["end"]) > float(iv["start"])
    ]

    def overlaps_keep(start: float, end: float) -> bool:
        for iv_start, iv_end in keep_ranges:
            if start < iv_end and end > iv_start:
                return True
        return False

    captions = []

    chunk: List[str] = []
    chunk_start = 0.0
    chunk_end = 0.0
    chunk_overlaps_keep = False

    for m_start, m_end, morpheme in morpheme_times:
        current_overlaps_keep = overlaps_keep(m_start, m_end)
        if chunk:
            speech_duration = chunk_end - chunk_start
            silence_gap = m_start - chunk_end
            crossed_keep_boundary = current_overlaps_keep != chunk_overlaps_keep
            should_flush = (
                (
                    (speech_duration > max_duration or len(chunk) >= max_morphemes)
                    and len(chunk) >= min_morphemes
                )
                or silence_gap > silence_flush
                or crossed_keep_boundary
            )

            if should_flush:
                captions.append(
                    {
                        "start": round(chunk_start, 3),
                        "end": round(chunk_end, 3),
                        "text": "".join(chunk),
                    }
                )
                chunk = []

        if not chunk:
            chunk_start = m_start
            chunk_overlaps_keep = current_overlaps_keep
        chunk.append(morpheme)
        chunk_end = m_end

    if chunk:
        captions.append(
            {
                "start": round(chunk_start, 3),
                "end": round(chunk_end, 3),
                "text": "".join(chunk),
            }
        )

    return captions


def apply_margins(
    intervals: List[dict],
    pre_margin: float,
    post_margin: float,
    duration_sec: float,
) -> List[dict]:
    """
    Expand each interval by pre_margin before start and post_margin
    after end, clamp to [0, duration_sec], then merge overlaps.
    """
    if not intervals:
        return intervals

    expanded = []
    for iv in intervals:
        start = max(0.0, iv["start"] - pre_margin)
        end = min(duration_sec, iv["end"] + post_margin)
        expanded.append({"start": start, "end": end})

    expanded.sort(key=lambda x: x["start"])

    merged = [expanded[0]]
    for iv in expanded[1:]:
        if iv["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], iv["end"])
        else:
            merged.append(iv)

    return merged


def main() -> None:
    args = parse_args()

    json_path = Path(args.json_path)
    config_path = Path(args.config_path)
    output_path = Path(args.output_path)

    with json_path.open("r", encoding="utf-8") as f:
        whisperx_data = json.load(f)

    filler_set = load_filler_set(config_path, args.language)
    tagger = Tagger("-Owakati")
    all_morpheme_times = build_morpheme_times(whisperx_data, tagger)

    words = all_morpheme_times
    speech_spans = build_speech_spans(whisperx_data)
    duration_sec = get_duration_sec(whisperx_data, words)

    excludes: List[Tuple[float, float]] = []

    for start, end, token in words:
        normalized = normalize_word(token)
        if normalized in filler_set:
            excludes.append((start - args.word_padding, end + args.word_padding))

    for idx in range(len(speech_spans) - 1):
        current_end = speech_spans[idx][1]
        next_start = speech_spans[idx + 1][0]
        gap = next_start - current_end
        if gap > args.silence_threshold:
            excludes.append((current_end, next_start))

    if speech_spans and speech_spans[0][0] > args.silence_threshold:
        excludes.append((0.0, speech_spans[0][0]))

    if speech_spans and (duration_sec - speech_spans[-1][1]) > args.silence_threshold:
        excludes.append((speech_spans[-1][1], duration_sec))

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

    keep_intervals = apply_margins(
        filtered_keep,
        pre_margin=args.pre_margin,
        post_margin=args.post_margin,
        duration_sec=duration_sec,
    )
    logging.info(
        "After margins (pre=%.2fs post=%.2fs): %d interval(s)",
        args.pre_margin,
        args.post_margin,
        len(keep_intervals),
    )

    captions = collect_captions(
        all_morpheme_times,
        keep_intervals,
        max_duration=args.caption_max_duration,
        max_morphemes=args.caption_max_morphemes,
        min_morphemes=args.caption_min_morphemes,
        min_duration=args.caption_min_duration,
        silence_flush=args.caption_silence_flush,
    )

    output_data = {
        "source_file": infer_source_file(whisperx_data, json_path),
        "duration_sec": round(duration_sec, 3),
        "keep_intervals": keep_intervals,
        "captions": captions,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
