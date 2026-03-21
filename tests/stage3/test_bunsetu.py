"""Tests for bunsetsu timing functions."""

import pytest
from unittest.mock import patch

from nagare_clip.stage3.bunsetu import build_bunsetu_times, flatten_bunsetu

from tests.stage3.conftest import make_nlp


def bunsetu_spans_from_doc(doc):
    """Side-effect for patching ginza.bunsetu_spans: returns spans stored on doc."""
    return doc._bunsetu_spans


# ---------------------------------------------------------------------------
# flatten_bunsetu (convenience wrapper)
# ---------------------------------------------------------------------------


def test_flatten_bunsetu_basic_two_bunsetu():
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
    nlp = make_nlp([["A", "B"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        words = build_bunsetu_times(whisperx_data, nlp)

    assert len(words) == 2
    assert words[0] == pytest.approx((0.0, 0.02, "A"), abs=1e-3)
    assert words[1] == pytest.approx((3.0, 3.02, "B"), abs=1e-3)


def test_flatten_bunsetu_intra_segment_no_silence():
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
    nlp = make_nlp([["A", "B"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        words = build_bunsetu_times(whisperx_data, nlp)

    assert len(words) == 2
    assert words[0] == pytest.approx((0.0, 0.02, "A"), abs=1e-3)
    assert words[1] == pytest.approx((0.02, 0.04, "B"), abs=1e-3)


def test_flatten_bunsetu_inter_segment_silence_preserved():
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
    nlp = make_nlp([["A"], ["B"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        words = build_bunsetu_times(whisperx_data, nlp)

    assert len(words) == 2
    # NOTE: inter-segment end is last_char_start + CHAR_EPS, NOT segment["end"].
    # Silence gap = 3.0 - 0.02 = 2.98 s.
    assert words[0][1] == pytest.approx(0.02, abs=1e-3)
    assert words[1][0] == pytest.approx(3.0, abs=1e-3)
    gap = words[1][0] - words[0][1]
    assert gap == pytest.approx(2.98, abs=1e-3)
    assert gap > 1.5


def test_flatten_bunsetu_placeholder_inherits_start():
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
    # GiNZA groups "AB" as a single bunsetsu.
    nlp = make_nlp([["AB"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        words = build_bunsetu_times(whisperx_data, nlp)

    assert len(words) == 1
    assert words[0] == pytest.approx((1.0, 1.02, "AB"), abs=1e-3)


# ---------------------------------------------------------------------------
# build_bunsetu_times — timing details
# ---------------------------------------------------------------------------


def test_build_bunsetu_times_ignores_inflated_end():
    """m_end is clamped by the next bunsetsu start, not the inflated char end."""
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
    nlp = make_nlp([["は", "こ"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        bunsetu = build_bunsetu_times(whisperx_data, nlp)

    assert bunsetu[0][1] == pytest.approx(12.876, abs=0.001)
    assert bunsetu[1][0] == pytest.approx(18.206, abs=0.001)


def test_build_bunsetu_times_real_silence_gap():
    """A large gap between two bunsetsu within the same segment is preserved."""
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
    # GiNZA groups "はい" and "ちょっと" as two bunsetsu.
    nlp = make_nlp([["はい", "ちょっと"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        bunsetu = build_bunsetu_times(whisperx_data, nlp)

    gap = bunsetu[1][0] - bunsetu[0][1]
    assert gap > 1.5


# ---------------------------------------------------------------------------
# Regression: intra-bunsetu gap hiding silence
# ---------------------------------------------------------------------------


def test_build_bunsetu_times_multichar_bunsetu_preserves_silence_gap():
    """
    Repro for real data: WhisperX char "あ" at 101.031 with a large gap before
    "と" at 107.74.  GiNZA might group them into a single bunsetsu "あと" (or
    "ねあと").  build_bunsetu_times must NOT produce a bunsetsu spanning
    (101.031, 107.76) because the 6.7 s silence between the two characters
    would be hidden.

    The intra-bunsetu gap snap detects the large gap inside the bunsetsu and
    snaps m_start forward to the later cluster (107.74), so the silence gap
    appears *before* "あと" instead of being hidden inside it.
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
    # GiNZA splits "ねあとは" -> ["ね", "あとは"] as bunsetsu.
    nlp = make_nlp([["ね", "あとは"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        bunsetu = build_bunsetu_times(whisperx_data, nlp)

    assert len(bunsetu) == 2
    ne, atowa = bunsetu

    assert ne[2] == "ね"
    assert atowa[2] == "あとは"

    # "あとは" must snap forward past the 6.7 s gap to the "と" cluster at 107.74.
    assert atowa[0] == pytest.approx(107.74, abs=1e-3)

    # Silence gap must appear between the two bunsetsu, not hidden inside.
    gap = atowa[0] - ne[1]
    assert gap > 1.5, (
        f"gap between 'ね' and 'あとは' is only {gap:.3f} s; "
        f"silence is hidden inside the bunsetsu"
    )
