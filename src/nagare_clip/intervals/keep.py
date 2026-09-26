"""Keep-interval computation: what the intervals stage plays, and what it drops.

:func:`compute_keep_intervals` is the whole chain from WhisperX word timings to
final keep intervals — word-gap silences over ``silence_threshold``,
leading/trailing silence, the audio_silence cut list, ``<keep>`` carve-outs,
``min_keep``, keep margins, caption coverage and the ``min_cut`` merge.
:func:`~nagare_clip.intervals.run.run_intervals` calls it, and so does
:func:`dropped_ranges`, which is what the director's brackets quote: a line's
``Ys silence`` is exactly the footage of that line the render will not play.
One chain, two callers, so the two can only disagree if the inputs do.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

from nagare_clip.edit_lines import gap_spans
from nagare_clip.intervals.bunsetu import build_bunsetu_times
from nagare_clip.intervals.captions import apply_caption_margins, collect_captions
from nagare_clip.intervals.intervals import (
    apply_margins,
    enforce_min_keep_duration,
    ensure_keep_covers_captions,
    invert_intervals,
    merge_close_intervals,
    merge_intervals,
    subtract_intervals,
)
from nagare_clip.intervals.speech import build_speech_spans, get_duration_sec
from nagare_clip.intervals.sync_json import (
    extract_cut_silences,
    extract_keep_ranges,
    sync_text_to_json,
)

Range = tuple[float, float]


@dataclass(frozen=True)
class KeepResult:
    keep_intervals: list[dict[str, float]]
    captions: list[dict[str, Any]]
    duration_sec: float


def compute_keep_intervals(
    whisperx_data: dict,
    all_bunsetu_times: list[tuple[float, float, str]],
    ivl: dict,
    *,
    cut_ranges: Sequence[Range] = (),
    force_keep_ranges: Sequence[Range] = (),
) -> KeepResult:
    """Final keep intervals (and the captions that shaped them) for one source."""
    cap = ivl["caption"]
    speech_spans = build_speech_spans(whisperx_data)
    duration_sec = get_duration_sec(whisperx_data, all_bunsetu_times)
    logging.info("Duration: %.1fs, bunsetsu: %d", duration_sec, len(all_bunsetu_times))

    excludes: list[Range] = []

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

    excludes.extend(cut_ranges)

    bounded_excludes = [
        (max(0.0, start), min(duration_sec, end)) for start, end in excludes if end > start
    ]
    # Only <keep> force-preserves audio. <speed> no longer carves silence out of
    # the excludes — it is purely a playback-speed annotation (its span is still
    # emitted verbatim in speed_ranges by the caller). To keep AND speed a
    # region, nest <speed> inside <keep>.
    all_force_keep: list[Range] = list(force_keep_ranges)
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

    return KeepResult(keep_intervals_dicts, captions, duration_sec)


@cache
def load_nlp() -> Any:
    """The GiNZA model, loaded once per process (bunsetu timing shapes captions)."""
    import spacy

    return spacy.load("ja_ginza")


def dropped_ranges(
    whisperx_data: dict,
    ivl: dict,
    *,
    edit_lines: Sequence[str] | None = None,
    cut_ranges: Sequence[Range] = (),
    nlp: Any = None,
) -> list[Range]:
    """Source ranges ``run_intervals`` would NOT play, with no director op.

    The complement of :func:`compute_keep_intervals`' result, fed exactly what
    the stage feeds it: *edit_lines* are synced into the JSON first (patches
    and ``<cut>`` deletions change the word timings) and their ``<keep>`` spans
    are honoured — on silence lines too, and a silence under ``<cut>`` is
    dropped; ``None`` means no edits exist yet (the summary stage).
    *nlp* defaults to :func:`load_nlp`.
    """
    data = whisperx_data
    force_keep: list[Range] = []
    cut_ranges = list(cut_ranges)
    if edit_lines is not None:
        lines = list(edit_lines)
        silences = gap_spans(whisperx_data)
        data = sync_text_to_json(whisperx_data, lines)
        force_keep = extract_keep_ranges(lines, data, silences=silences)
        cut_ranges += extract_cut_silences(lines, silences)
    bun = ivl["bunsetu"]
    bunsetu = build_bunsetu_times(
        data,
        load_nlp() if nlp is None else nlp,
        char_eps=bun["char_eps"],
        silence_max_word_span=bun["silence_max_word_span"],
    )
    result = compute_keep_intervals(
        data, bunsetu, ivl, cut_ranges=cut_ranges, force_keep_ranges=force_keep
    )
    keeps = [(k["start"], k["end"]) for k in result.keep_intervals]
    return [(s, e) for s, e in invert_intervals(merge_intervals(keeps), result.duration_sec)]
