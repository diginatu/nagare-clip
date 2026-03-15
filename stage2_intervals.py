#!/usr/bin/env python3

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import yaml


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


def flatten_words(whisperx_data: dict) -> List[Tuple[float, float, str]]:
    words: List[Tuple[float, float, str]] = []
    for segment in whisperx_data.get("segments", []):
        for word in segment.get("words", []):
            start = word.get("start")
            end = word.get("end")
            token = word.get("word", "")
            if start is None or end is None:
                continue
            start_f = float(start)
            end_f = float(end)
            if end_f <= start_f:
                continue
            words.append((start_f, end_f, token))
    words.sort(key=lambda item: item[0])
    return words


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
    whisperx_data: dict,
    keep_intervals: List[dict],
) -> List[dict]:
    captions = []
    for segment in whisperx_data.get("segments", []):
        seg_start = segment.get("start")
        seg_end = segment.get("end")
        text = segment.get("text", "").strip()
        if seg_start is None or seg_end is None or not text:
            continue
        seg_start = float(seg_start)
        seg_end = float(seg_end)
        for iv in keep_intervals:
            if seg_start < iv["end"] and seg_end > iv["start"]:
                captions.append(
                    {
                        "start": round(seg_start, 3),
                        "end": round(seg_end, 3),
                        "text": text,
                    }
                )
                break
    return captions


def main() -> None:
    args = parse_args()

    json_path = Path(args.json_path)
    config_path = Path(args.config_path)
    output_path = Path(args.output_path)

    with json_path.open("r", encoding="utf-8") as f:
        whisperx_data = json.load(f)

    filler_set = load_filler_set(config_path, args.language)
    words = flatten_words(whisperx_data)
    duration_sec = get_duration_sec(whisperx_data, words)

    excludes: List[Tuple[float, float]] = []

    for start, end, token in words:
        normalized = normalize_word(token)
        if normalized in filler_set:
            excludes.append((start - args.word_padding, end + args.word_padding))

    for idx in range(len(words) - 1):
        current_end = words[idx][1]
        next_start = words[idx + 1][0]
        gap = next_start - current_end
        if gap > args.silence_threshold:
            excludes.append((current_end, next_start))

    if words and words[0][0] > args.silence_threshold:
        excludes.append((0.0, words[0][0]))

    if words and (duration_sec - words[-1][1]) > args.silence_threshold:
        excludes.append((words[-1][1], duration_sec))

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

    captions = collect_captions(whisperx_data, filtered_keep)

    output_data = {
        "source_file": infer_source_file(whisperx_data, json_path),
        "duration_sec": round(duration_sec, 3),
        "keep_intervals": filtered_keep,
        "captions": captions,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
