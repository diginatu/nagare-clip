"""Tests for morpheme timing functions."""

import pytest
from unittest.mock import patch

from video_editor_ai.stage2.morpheme import build_morpheme_times, flatten_words

from tests.stage2.conftest import make_tagger


# flatten_words


def test_flatten_words_basic_two_morphemes():
    whisperx_data = {
        "segments": [
            {
                "start": 0.0,
                "end": 5.0,
                "text": "AB",
                "words": [
                    {"word": "A", "start": 0.0, "score": 0.9},
                    {"word": "B", "start": 3.0, "score": 0.9},
                ],
            }
        ]
    }
    tagger = make_tagger([["A", "B"]])
    with patch("fugashi.Tagger", return_value=tagger):
        words = flatten_words(whisperx_data)

    assert len(words) == 2
    assert words[0] == pytest.approx((0.0, 0.02, "A"), abs=1e-3)
    assert words[1] == pytest.approx((3.0, 3.02, "B"), abs=1e-3)


def test_flatten_words_intra_segment_no_silence():
    whisperx_data = {
        "segments": [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "AB",
                "words": [
                    {"word": "A", "start": 0.0, "score": 0.9},
                    {"word": "B", "start": 0.02, "score": 0.9},
                ],
            }
        ]
    }
    tagger = make_tagger([["A", "B"]])
    with patch("fugashi.Tagger", return_value=tagger):
        words = flatten_words(whisperx_data)

    assert len(words) == 2
    assert words[0] == pytest.approx((0.0, 0.02, "A"), abs=1e-3)
    assert words[1] == pytest.approx((0.02, 0.04, "B"), abs=1e-3)


def test_flatten_words_inter_segment_silence_preserved():
    whisperx_data = {
        "segments": [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "A",
                "words": [{"word": "A", "start": 0.0, "score": 0.9}],
            },
            {
                "start": 3.0,
                "end": 4.0,
                "text": "B",
                "words": [{"word": "B", "start": 3.0, "score": 0.9}],
            },
        ]
    }
    tagger = make_tagger([["A"], ["B"]])
    with patch("fugashi.Tagger", return_value=tagger):
        words = flatten_words(whisperx_data)

    assert len(words) == 2
    # NOTE: inter-segment end is last_char_start + CHAR_EPS, NOT segment["end"].
    # The silence gap is detected via next_segment.first_char.start - this_segment.last_char.end,
    # which equals 3.0 - 0.02 = 2.98s here. segment["end"] is not used by build_morpheme_times.
    assert words[0][1] == pytest.approx(0.02, abs=1e-3)
    assert words[1][0] == pytest.approx(3.0, abs=1e-3)
    gap = words[1][0] - words[0][1]
    assert gap == pytest.approx(2.98, abs=1e-3)
    assert gap > 1.5


def test_flatten_words_placeholder_inherits_start():
    whisperx_data = {
        "segments": [
            {
                "start": 0.0,
                "end": 5.0,
                "text": "AB",
                "words": [
                    {"word": "A", "start": 1.0, "score": 0.9},
                    {"word": "B", "start": None, "score": 0.0},
                ],
            }
        ]
    }
    tagger = make_tagger([["AB"]])
    with patch("fugashi.Tagger", return_value=tagger):
        words = flatten_words(whisperx_data)

    assert len(words) == 1
    assert words[0] == pytest.approx((1.0, 1.02, "AB"), abs=1e-3)


# build_morpheme_times


def test_build_morpheme_times_ignores_inflated_end():
    whisperx_data = {
        "segments": [
            {
                "start": 5.0,
                "end": 20.0,
                "text": "はこ",
                "words": [
                    {"word": "は", "start": 12.856, "score": 0.983},
                    {"word": "こ", "start": 18.206, "score": 0.0},
                ],
            }
        ]
    }
    tagger = make_tagger([["は", "こ"]])
    morphemes = build_morpheme_times(whisperx_data, tagger)

    assert morphemes[0][1] == pytest.approx(12.876, abs=0.001)
    assert morphemes[1][0] == pytest.approx(18.206, abs=0.001)


def test_build_morpheme_times_real_silence_gap():
    whisperx_data = {
        "segments": [
            {
                "start": 30.0,
                "end": 55.0,
                "text": "はいちょっと",
                "words": [
                    {"word": "は", "start": 34.496, "score": 0.781},
                    {"word": "い", "start": 34.596, "score": 0.991},
                    {"word": "ち", "start": 53.545, "score": 0.0},
                    {"word": "ょ", "start": 53.565, "score": 0.0},
                    {"word": "っ", "start": 53.585, "score": 0.0},
                    {"word": "と", "start": 53.605, "score": 0.0},
                ],
            }
        ]
    }
    tagger = make_tagger([["はい", "ちょっと"]])
    morphemes = build_morpheme_times(whisperx_data, tagger)

    gap = morphemes[1][0] - morphemes[0][1]
    assert gap > 1.5


# Regression: multi-char morpheme hiding silence gap


def test_build_morpheme_times_multichar_morpheme_preserves_silence_gap():
    """
    Repro for real data: WhisperX char "あ" at 101.031 with inflated end 107.74,
    followed by "と" at 107.74.  fugashi groups them into a single morpheme "あと".
    build_morpheme_times must NOT produce a morpheme spanning (101.031, 107.76)
    because the 6.7 s silence between the two characters would be hidden.

    WhisperX misaligned the first char "あ" to 101.031; the real utterance of
    "あと" is at ~107.74 (the "と" cluster).  The fix detects the large
    intra-morpheme gap and snaps the morpheme start forward to the later
    cluster, so the silence gap appears *before* "あと" instead of being
    hidden inside it.
    """
    whisperx_data = {
        "segments": [
            {
                "start": 100.0,
                "end": 110.0,
                "text": "ねあとは",
                "words": [
                    {"word": "ね", "start": 101.011, "end": 101.031, "score": 0.0},
                    {"word": "あ", "start": 101.031, "end": 107.74, "score": 0.978},
                    {"word": "と", "start": 107.74, "end": 107.84, "score": 0.601},
                    {"word": "は", "start": 107.84, "end": 108.541, "score": 0.679},
                ],
            }
        ]
    }
    # fugashi tokenizes "ねあとは" -> ["ね", "あと", "は"]
    tagger = make_tagger([["ね", "あと", "は"]])
    morphemes = build_morpheme_times(whisperx_data, tagger)

    assert len(morphemes) == 3
    ne, ato, ha = morphemes

    assert ne[2] == "ね"
    assert ato[2] == "あと"
    assert ha[2] == "は"

    # "あと" should snap forward to the later cluster ("と" at 107.74),
    # because the gap between "あ" (101.031) and "と" (107.74) exceeds
    # SILENCE_MAX_WORD_SPAN and "あ" is the misaligned character.
    assert ato[0] == pytest.approx(107.74, abs=1e-3)
    assert ato[1] == pytest.approx(107.76, abs=1e-3)

    # The silence gap should now appear between "ね" and "あと"
    gap = ato[0] - ne[1]
    assert gap > 1.5, (
        f"gap between 'ね' and 'あと' is only {gap:.3f} s; "
        f"silence is hidden inside the morpheme"
    )
