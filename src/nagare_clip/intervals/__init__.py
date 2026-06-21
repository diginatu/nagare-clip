"""intervals: Patch application + keep-interval computation."""

from nagare_clip.intervals.bunsetu import (
    CHAR_EPS,
    SILENCE_MAX_WORD_SPAN,
    build_bunsetu_times,
    flatten_bunsetu,
)
from nagare_clip.intervals.captions import collect_captions
from nagare_clip.intervals.intervals import (
    apply_margins,
    enforce_min_keep_duration,
    ensure_keep_covers_captions,
    invert_intervals,
    merge_intervals,
)
from nagare_clip.intervals.io import infer_source_file
from nagare_clip.intervals.speech import build_speech_spans, get_duration_sec

__all__ = [
    "CHAR_EPS",
    "SILENCE_MAX_WORD_SPAN",
    "apply_margins",
    "build_bunsetu_times",
    "build_speech_spans",
    "collect_captions",
    "enforce_min_keep_duration",
    "ensure_keep_covers_captions",
    "flatten_bunsetu",
    "get_duration_sec",
    "infer_source_file",
    "invert_intervals",
    "merge_intervals",
]
