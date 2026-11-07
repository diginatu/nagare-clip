"""Tests for Silero VAD result handling."""

import json

from nagare_clip.audio_silence.vad import join_close_regions, read_vad, silences_from_speech


def test_silences_complement():
    assert silences_from_speech([(1.0, 2.0), (2.2, 5.0)], 6.0, 0.5) == [(0.0, 1.0), (5.0, 6.0)]


def test_no_speech_is_one_silence():
    assert silences_from_speech([], 4.0, 0.5) == [(0.0, 4.0)]


def test_read_vad(tmp_path):
    p = tmp_path / "a_vad.json"
    p.write_text(json.dumps({"duration": 6.0, "speech": [[1.0, 2.0], [2.2, 5.0]]}))
    assert read_vad(p) == (6.0, [(1.0, 2.0), (2.2, 5.0)])


def test_join_gap_below_min_silence_merges():
    assert join_close_regions([(1.0, 2.0), (2.4, 3.0)], 0.5) == [(1.0, 3.0)]


def test_join_gap_equal_min_silence_does_not_merge():
    assert join_close_regions([(1.0, 2.0), (2.5, 3.0)], 0.5) == [(1.0, 2.0), (2.5, 3.0)]


def test_join_empty():
    assert join_close_regions([], 0.5) == []


def test_join_overlapping_and_unsorted():
    assert join_close_regions([(5.0, 6.0), (1.0, 3.0), (2.0, 2.5)], 0.5) == [(1.0, 3.0), (5.0, 6.0)]


def test_join_keeps_running_max_end():
    # (1,10) swallows (2,3); (10.2, 11) is within 0.5 of the running end 10
    assert join_close_regions([(1.0, 10.0), (2.0, 3.0), (10.2, 11.0)], 0.5) == [(1.0, 11.0)]


def test_join_gaps_equal_internal_silences():
    cases = [
        [(1.0, 2.0), (2.4, 3.0), (3.5, 4.0), (9.0, 9.2)],
        [(0.0, 1.0), (1.49, 2.0), (2.5, 3.0)],
        [(5.0, 6.0), (1.0, 3.0), (2.0, 2.5), (3.4, 3.45)],
        [(2.0, 2.1)],
    ]
    for speech in cases:
        for min_silence in (0.3, 0.5, 1.0):
            joined = join_close_regions(speech, min_silence)
            gaps = [(a[1], b[0]) for a, b in zip(joined, joined[1:], strict=False)]
            duration = 100.0
            internal = [
                s
                for s in silences_from_speech(speech, duration, min_silence)
                if s[0] > 0.0 and s[1] < duration
            ]
            assert gaps == internal, (speech, min_silence)
