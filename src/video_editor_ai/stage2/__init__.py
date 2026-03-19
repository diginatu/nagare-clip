"""Stage 2: Keep-interval computation from WhisperX transcripts."""

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
from video_editor_ai.stage2.morpheme import (
    CHAR_EPS,
    SILENCE_MAX_WORD_SPAN,
    build_morpheme_times,
    flatten_words,
)
from video_editor_ai.stage2.speech import build_speech_spans, get_duration_sec

__all__ = [
    "CHAR_EPS",
    "SILENCE_MAX_WORD_SPAN",
    "apply_margins",
    "build_morpheme_times",
    "build_speech_spans",
    "collect_captions",
    "enforce_min_keep_duration",
    "ensure_keep_covers_captions",
    "flatten_words",
    "get_duration_sec",
    "infer_source_file",
    "invert_intervals",
    "load_filler_set",
    "merge_intervals",
    "normalize_word",
]
