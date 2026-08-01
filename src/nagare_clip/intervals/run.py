"""intervals stage: apply patches, sync JSON, compute keep intervals."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import spacy

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.intervals.bunsetu import build_bunsetu_times, bunsetu_join_text
from nagare_clip.intervals.captions import apply_caption_margins, collect_captions
from nagare_clip.intervals.intervals import (
    apply_margins,
    enforce_min_keep_duration,
    ensure_keep_covers_captions,
    invert_intervals,
    merge_close_intervals,
    merge_intervals,
    snap_overlay_starts,
    subtract_intervals,
)
from nagare_clip.intervals.io import infer_source_file
from nagare_clip.intervals.speech import build_speech_spans, get_duration_sec
from nagare_clip.intervals.sync_json import (
    extract_keep_ranges,
    extract_overlay_marks,
    extract_speed_ranges,
    sync_text_to_json,
)
from nagare_clip.timing import segment_times


def run_intervals(
    edits_txt: Path,
    json_path: Path,
    output: Path,
    cfg: dict,
    *,
    cuts_txt: Path | None = None,
) -> None:
    ivl = cfg["intervals"]
    cap = ivl["caption"]
    bun = ivl["bunsetu"]

    # --- Sync edit lines → JSON (applies {{old->new}} patches internally) ---
    edit_lines = edits_txt.read_text(encoding="utf-8").splitlines()
    logging.info("intervals: syncing edits from %s", edits_txt.name)

    with json_path.open("r", encoding="utf-8") as f:
        whisperx_data = json.load(f)

    whisperx_data = sync_text_to_json(whisperx_data, edit_lines)
    force_keep_ranges = extract_keep_ranges(edit_lines, whisperx_data)
    speed_ranges = extract_speed_ranges(edit_lines, whisperx_data)
    overlay_marks = extract_overlay_marks(edit_lines, whisperx_data)
    if force_keep_ranges:
        logging.info("Force-keep ranges from <keep>: %d", len(force_keep_ranges))
    if speed_ranges:
        logging.info("Speed ranges from <speed>: %d", len(speed_ranges))
    if overlay_marks:
        logging.info("Overlay marks from <overlay/>: %d", len(overlay_marks))

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

    excludes: list[tuple[float, float]] = []

    silence_excludes = 0
    for idx in range(len(speech_spans) - 1):
        current_end = speech_spans[idx][1]
        next_start = speech_spans[idx + 1][0]
        gap = next_start - current_end
        if gap > ivl["silence_threshold"]:
            logging.debug("Silence gap: %.3f-%.3f (%.3fs)", current_end, next_start, gap)
            excludes.append((current_end, next_start))
            silence_excludes += 1

    if speech_spans and speech_spans[0][0] > ivl["silence_threshold"]:
        logging.debug(
            "Silence gap: 0.000-%.3f (%.3fs) [leading]",
            speech_spans[0][0],
            speech_spans[0][0],
        )
        excludes.append((0.0, speech_spans[0][0]))
        silence_excludes += 1

    if speech_spans and (duration_sec - speech_spans[-1][1]) > ivl["silence_threshold"]:
        logging.debug(
            "Silence gap: %.3f-%.3f (%.3fs) [trailing]",
            speech_spans[-1][1],
            duration_sec,
            duration_sec - speech_spans[-1][1],
        )
        excludes.append((speech_spans[-1][1], duration_sec))
        silence_excludes += 1

    logging.info("Silence excluded: %d interval(s)", silence_excludes)

    if cuts_txt:
        cut_ranges = read_cuts(Path(cuts_txt))
        excludes.extend(cut_ranges)
        logging.info(
            "Audio-silence cuts unioned: %d range(s) from %s",
            len(cut_ranges),
            Path(cuts_txt).name,
        )

    bounded_excludes = [
        (max(0.0, start), min(duration_sec, end)) for start, end in excludes if end > start
    ]
    # Only <keep> force-preserves audio. <speed> no longer carves silence out of
    # the excludes — it is purely a playback-speed annotation (its span is still
    # emitted verbatim in speed_ranges below). To keep AND speed a region, nest
    # <speed> inside <keep>.
    all_force_keep: list[tuple[float, float]] = list(force_keep_ranges)
    if all_force_keep:
        bounded_excludes = subtract_intervals(bounded_excludes, all_force_keep)
    merged_excludes = merge_intervals(bounded_excludes)
    keep_intervals = invert_intervals(merged_excludes, duration_sec)
    filtered_keep = [
        {"start": round(start, 3), "end": round(end, 3)}
        for start, end in keep_intervals
        if (end - start) >= ivl["min_keep"]
    ]
    logging.info("Keep intervals before margins: %d", len(filtered_keep))

    keep_intervals_dicts = apply_margins(
        filtered_keep,
        pre_margin=ivl["keep_pre_margin"],
        post_margin=ivl["keep_post_margin"],
        duration_sec=duration_sec,
    )
    logging.info(
        "After keep margins (pre=%.2fs post=%.2fs): %d interval(s)",
        ivl["keep_pre_margin"],
        ivl["keep_post_margin"],
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
        ivl["min_keep"],
        duration_sec,
    )
    logging.info("After min_keep enforcement: %d interval(s)", len(keep_intervals_dicts))

    # Runs last: every earlier pass only expands intervals, so this is the one
    # position where the gap being measured is the final gap.
    if ivl["min_cut"] > 0.0:
        keep_intervals_dicts = merge_close_intervals(keep_intervals_dicts, ivl["min_cut"])
        logging.info(
            "After min_cut merge (gaps < %.2fs absorbed): %d interval(s)",
            ivl["min_cut"],
            len(keep_intervals_dicts),
        )

    if overlay_marks:
        # Runs after every keep-interval pass: the anchor is snapped against the
        # final intervals, so a caption whose line opened on cut footage moves to
        # the line's first surviving moment instead of being dropped in blender.
        moved = snap_overlay_starts(
            overlay_marks, segment_times(whisperx_data), keep_intervals_dicts
        )
        for (before, _, text), (after, _, _) in zip(overlay_marks, moved):
            if after != before:
                logging.info(
                    "Overlay anchor snapped %.3f -> %.3f (cut line opening): %r",
                    before,
                    after,
                    text[:40],
                )
        overlay_marks = moved

        # Blender's TEXT strip wraps only at whitespace; free-form overlay
        # text otherwise has none. Reuses the caption separator/nlp already
        # loaded above rather than adding a second config knob.
        overlay_marks = [
            (start, duration, bunsetu_join_text(text, nlp, cap["bunsetu_separator"]))
            for start, duration, text in overlay_marks
        ]

    output_data = {
        "source_file": infer_source_file(whisperx_data, json_path),
        "duration_sec": round(duration_sec, 3),
        "keep_intervals": keep_intervals_dicts,
        "captions": captions,
    }
    if speed_ranges:
        # Speed ranges are emitted as an independent top-level array (like
        # overlays); the blender stage splits keep intervals at these boundaries so a
        # speed range may cover an arbitrary sub-range of a keep interval.
        output_data["speed_ranges"] = [
            {"start": round(s, 3), "end": round(e, 3), "factor": f} for s, e, f in speed_ranges
        ]
    if overlay_marks:
        # An overlay is a point + a duration in *edited-timeline* seconds; the
        # blender stage measures it in output frames, so cuts and speed ranges
        # inside the window cannot shorten the reading time.
        output_data["overlays"] = [
            {"start": round(s, 3), "duration": round(d, 3), "text": t} for s, d, t in overlay_marks
        ]

    output.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Writing output to %s", output)
    with output.open("w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
        f.write("\n")
