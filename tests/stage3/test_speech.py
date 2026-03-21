"""Tests for speech span extraction."""

import pytest

from nagare_clip.stage3.speech import build_speech_spans


def test_build_speech_spans_caps_inflated_word_end_for_gap_detection():
    whisperx_data = {
        "segments": [
            {
                "text": "ICに",
                "words": [
                    {"word": "I", "start": 75.053, "end": 75.073},
                    {"word": "C", "start": 75.073, "end": 80.06},
                    {"word": "に", "start": 80.06, "end": 80.08},
                ],
            }
        ]
    }

    spans = build_speech_spans(whisperx_data)

    ic_idx = next(
        i
        for i, (start, _) in enumerate(spans)
        if start == pytest.approx(75.073, abs=1e-3)
    )
    assert spans[ic_idx][1] == pytest.approx(75.673, abs=1e-3)
    gap = spans[ic_idx + 1][0] - spans[ic_idx][1]
    assert gap == pytest.approx(4.387, abs=1e-3)
