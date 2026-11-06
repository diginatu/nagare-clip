"""Tests for the audio_silence stage run() function."""

import json

from nagare_clip.audio_silence.cuts_file import read_cuts, read_media_duration
from nagare_clip.audio_silence.run import run_audio_silence
from nagare_clip.config import get_effective_config


def _vad(tmp_path, duration=6.0, speech=((1.0, 2.0), (2.2, 5.0))):
    p = tmp_path / "clip_vad.json"
    p.write_text(json.dumps({"duration": duration, "speech": [list(s) for s in speech]}))
    return p


def test_run_writes_silences_and_duration(tmp_path):
    out = tmp_path / "clip_cuts.txt"
    run_audio_silence(out, get_effective_config(None, {}), vad_path=_vad(tmp_path))
    assert read_media_duration(out) == 6.0
    assert read_cuts(out) == [(0.0, 1.0), (5.0, 6.0)]


def test_min_silence_comes_from_cfg(tmp_path):
    out = tmp_path / "clip_cuts.txt"
    cfg = get_effective_config(None, {"audio_silence": {"min_silence": 1.5}})
    run_audio_silence(out, cfg, vad_path=_vad(tmp_path))
    assert read_cuts(out) == []
