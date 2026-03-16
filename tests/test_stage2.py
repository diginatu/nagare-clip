import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

import stage2_intervals as s


def make_tagger(morpheme_lists):
    """
    morpheme_lists: list of lists of surface strings, one per tagger call.
    """

    call_iter = iter(morpheme_lists)

    def side_effect(text):
        surfaces = next(call_iter)
        return [MagicMock(surface=surface) for surface in surfaces]

    return MagicMock(side_effect=side_effect)


# apply_margins


def test_apply_margins_basic_expansion():
    intervals = [{"start": 5.0, "end": 10.0}]
    result = s.apply_margins(intervals, 1.0, 1.0, 20.0)
    assert result == [{"start": 4.0, "end": 11.0}]


def test_apply_margins_clamps_bounds():
    intervals = [{"start": 0.5, "end": 19.5}]
    result = s.apply_margins(intervals, 1.0, 1.0, 20.0)
    assert result == [{"start": 0.0, "end": 20.0}]


def test_apply_margins_no_merge_after_expansion():
    intervals = [{"start": 5.0, "end": 10.0}, {"start": 13.0, "end": 18.0}]
    result = s.apply_margins(intervals, 1.0, 1.0, 30.0)
    assert result == [{"start": 4.0, "end": 11.0}, {"start": 12.0, "end": 19.0}]


def test_apply_margins_merges_after_expansion():
    intervals = [{"start": 5.0, "end": 10.0}, {"start": 11.5, "end": 18.0}]
    result = s.apply_margins(intervals, 1.0, 1.0, 30.0)
    assert result == [{"start": 4.0, "end": 19.0}]


def test_apply_margins_empty_input():
    assert s.apply_margins([], 1.0, 1.0, 20.0) == []


def test_apply_margins_zero_margins_no_change():
    intervals = [{"start": 5.0, "end": 10.0}]
    result = s.apply_margins(intervals, 0.0, 0.0, 20.0)
    assert result == intervals


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
        words = s.flatten_words(whisperx_data)

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
        words = s.flatten_words(whisperx_data)

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
        words = s.flatten_words(whisperx_data)

    assert len(words) == 2
    # NOTE: inter-segment end is last_char_start + _CHAR_EPS, NOT segment["end"].
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
        words = s.flatten_words(whisperx_data)

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
    morphemes = s.build_morpheme_times(whisperx_data, tagger)

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
    morphemes = s.build_morpheme_times(whisperx_data, tagger)

    gap = morphemes[1][0] - morphemes[0][1]
    assert gap > 1.5


# collect_captions


def test_collect_captions_flush_and_preserve_silence_split_chunks():
    morphemes = [
        (0.0, 0.5, "あ"),
        (0.6, 1.0, "い"),
        (3.3, 3.5, "う"),
    ]
    keep_intervals = [{"start": 0.0, "end": 2.0}]

    captions = s.collect_captions(
        morphemes,
        keep_intervals,
        max_duration=4.0,
        max_morphemes=12,
        min_morphemes=1,
        min_duration=0.0,
        silence_flush=1.5,
    )

    assert len(captions) == 2
    assert captions[0]["text"] == "あい"
    assert captions[0]["start"] == pytest.approx(0.0)
    assert captions[0]["end"] == pytest.approx(1.0)
    assert captions[1]["text"] == "う"
    assert captions[1]["start"] == pytest.approx(3.3)
    assert captions[1]["end"] == pytest.approx(3.5)


def test_build_speech_spans_caps_inflated_word_end_for_gap_detection():
    whisperx_path = Path("tests/fixtures/whisperx_segment_motor.json")
    whisperx_data = json.loads(whisperx_path.read_text())

    spans = s.build_speech_spans(whisperx_data)

    ic_idx = next(
        i
        for i, (start, _) in enumerate(spans)
        if start == pytest.approx(75.073, abs=1e-3)
    )
    assert spans[ic_idx][1] == pytest.approx(75.673, abs=1e-3)
    gap = spans[ic_idx + 1][0] - spans[ic_idx][1]
    assert gap == pytest.approx(4.387, abs=1e-3)


def test_collect_captions_splits_on_keep_boundary_without_silence():
    morphemes = [
        (0.0, 0.3, "あ"),
        (0.3, 0.6, "い"),
        (0.6, 0.9, "う"),
    ]
    keep_intervals = [{"start": 0.0, "end": 0.6}]

    captions = s.collect_captions(
        morphemes,
        keep_intervals,
        max_duration=10.0,
        max_morphemes=12,
        min_morphemes=1,
        min_duration=0.0,
        silence_flush=10.0,
    )

    assert [cap["text"] for cap in captions] == ["あい", "う"]


# apply_margins integration


def test_apply_margins_real_intervals_no_merge():
    intervals = [
        {"start": 5.903, "end": 87.43},
        {"start": 89.857, "end": 109.502},
    ]
    result = s.apply_margins(intervals, 1.0, 1.0, 109.502)

    assert len(result) == 2
    assert result[0] == pytest.approx({"start": 4.903, "end": 88.43})
    assert result[1] == pytest.approx({"start": 88.857, "end": 109.502})


def test_apply_margins_real_intervals_merge_with_larger_padding():
    intervals = [
        {"start": 5.903, "end": 87.43},
        {"start": 89.857, "end": 109.502},
    ]
    result = s.apply_margins(intervals, 2.0, 2.0, 109.502)

    assert len(result) == 1
    assert result[0] == pytest.approx({"start": 3.903, "end": 109.502})
