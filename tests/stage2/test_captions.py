"""Tests for caption chunking functions."""

import pytest

from video_editor_ai.stage2.captions import collect_captions


def test_collect_captions_flush_and_preserve_silence_split_chunks():
    morphemes = [
        (0.0, 0.5, "あ"),
        (0.6, 1.0, "い"),
        (3.3, 3.5, "う"),
    ]
    keep_intervals = [{"start": 0.0, "end": 2.0}]

    captions = collect_captions(
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


def test_collect_captions_splits_on_keep_boundary_without_silence():
    morphemes = [
        (0.0, 0.3, "あ"),
        (0.3, 0.6, "い"),
        (0.6, 0.9, "う"),
    ]
    keep_intervals = [{"start": 0.0, "end": 0.6}]

    captions = collect_captions(
        morphemes,
        keep_intervals,
        max_duration=10.0,
        max_morphemes=12,
        min_morphemes=1,
        min_duration=0.0,
        silence_flush=10.0,
    )

    assert [cap["text"] for cap in captions] == ["あい", "う"]
