"""Tests for Silero VAD result handling."""

import json

from nagare_clip.audio_silence.vad import read_vad, silences_from_speech


def test_silences_complement():
    assert silences_from_speech([(1.0, 2.0), (2.2, 5.0)], 6.0, 0.5) == [(0.0, 1.0), (5.0, 6.0)]


def test_no_speech_is_one_silence():
    assert silences_from_speech([], 4.0, 0.5) == [(0.0, 4.0)]


def test_read_vad(tmp_path):
    p = tmp_path / "a_vad.json"
    p.write_text(json.dumps({"duration": 6.0, "speech": [[1.0, 2.0], [2.2, 5.0]]}))
    assert read_vad(p) == (6.0, [(1.0, 2.0), (2.2, 5.0)])
