"""Tests for caption chunking functions."""

import pytest

from video_editor_ai.stage2.captions import collect_captions, expand_short_captions


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


# --- expand_short_captions tests ---


def test_expand_short_symmetric_between_neighbors():
    """Tiny caption with room on both sides expands symmetrically."""
    captions = [
        {"start": 0.0, "end": 1.5, "text": "前"},
        {"start": 5.0, "end": 5.2, "text": "短"},  # 0.2s, min=1.5 → need 1.3 more
        {"start": 10.0, "end": 11.5, "text": "後"},
    ]
    result = expand_short_captions(captions, min_duration=1.5, duration_sec=20.0)
    assert result[0] == captions[0]
    assert result[2] == captions[2]
    short = result[1]
    assert short["text"] == "短"
    # Symmetric: 1.3/2 = 0.65 each side → start=4.35, end=5.85
    assert short["start"] == pytest.approx(4.35)
    assert short["end"] == pytest.approx(5.85)
    assert short["end"] - short["start"] == pytest.approx(1.5)


def test_expand_short_zero_backward_room():
    """Caption immediately after previous: all expansion goes forward."""
    captions = [
        {"start": 0.0, "end": 5.0, "text": "前"},
        {
            "start": 5.0,
            "end": 5.1,
            "text": "短",
        },  # 0.1s, prev ends at 5.0 (no back room)
        {"start": 10.0, "end": 11.5, "text": "後"},
    ]
    result = expand_short_captions(captions, min_duration=1.5, duration_sec=20.0)
    short = result[1]
    assert short["start"] == pytest.approx(5.0)  # clamped to prev end
    assert short["end"] == pytest.approx(6.5)  # full 1.5 pushed forward
    assert short["end"] - short["start"] == pytest.approx(1.5)


def test_expand_short_clamped_at_start_zero():
    """First caption near t=0: surplus backward margin moves to end."""
    captions = [
        {"start": 0.1, "end": 0.2, "text": "短"},  # 0.1s, only 0.1s available backward
        {"start": 5.0, "end": 6.0, "text": "後"},
    ]
    result = expand_short_captions(captions, min_duration=1.5, duration_sec=20.0)
    short = result[0]
    assert short["start"] == pytest.approx(0.0)  # clamped to 0
    # half=0.7 back, but only 0.1 available → 0.6 extra pushed forward
    # new_end = min(0.2 + 0.7 + 0.6, 5.0) = 1.5
    assert short["end"] == pytest.approx(1.5)
    assert short["end"] - short["start"] == pytest.approx(1.5)


def test_expand_short_last_caption_uses_duration_sec():
    """Last caption expands up to duration_sec as upper bound."""
    captions = [
        {"start": 0.0, "end": 1.0, "text": "前"},
        {"start": 9.9, "end": 10.0, "text": "短"},  # 0.1s, last caption
    ]
    result = expand_short_captions(captions, min_duration=1.5, duration_sec=10.0)
    short = result[1]
    # half=0.7 each side → new_start=9.2, new_end clamped to 10.0
    # leftover_fwd=0.7 fully pushed backward: new_start = 9.2 - 0.7 = 8.5
    assert short["start"] == pytest.approx(8.5)
    assert short["end"] == pytest.approx(10.0)
    assert short["end"] - short["start"] == pytest.approx(1.5)


def test_expand_short_both_sides_constrained():
    """Caption squeezed between two close neighbors: expands as much as possible."""
    captions = [
        {"start": 0.0, "end": 4.8, "text": "前"},
        {
            "start": 4.9,
            "end": 5.0,
            "text": "短",
        },  # 0.1s, only 0.1s back, 0.1s fwd available
        {"start": 5.1, "end": 6.5, "text": "後"},
    ]
    result = expand_short_captions(captions, min_duration=1.5, duration_sec=20.0)
    short = result[1]
    # Can only reach 4.8–5.1 = 0.3s total (both neighbors block further expansion)
    assert short["start"] == pytest.approx(4.8)
    assert short["end"] == pytest.approx(5.1)


def test_expand_short_already_long_enough():
    """Captions meeting min_duration are not modified."""
    captions = [
        {"start": 0.0, "end": 2.0, "text": "長い"},
    ]
    result = expand_short_captions(captions, min_duration=1.5, duration_sec=10.0)
    assert result == captions


def test_expand_short_min_duration_zero_noop():
    """min_duration=0 means no expansion regardless of caption length."""
    captions = [
        {"start": 1.0, "end": 1.01, "text": "短"},
    ]
    result = expand_short_captions(captions, min_duration=0.0, duration_sec=10.0)
    assert result == captions
