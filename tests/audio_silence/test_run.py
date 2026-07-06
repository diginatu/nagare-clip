"""Tests for the audio_silence stage run() function."""

import yaml

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.audio_silence.run import run_audio_silence
from nagare_clip.config import get_effective_config

_RAW = """\
    Duration: 00:00:20.00, start: 0.000000
[silencedetect @ 0x55] silence_start: 2.5
[silencedetect @ 0x55] silence_end: 4.0 | silence_duration: 1.5
[silencedetect @ 0x55] silence_start: 18.0
"""


def test_without_raw_writes_header_only(tmp_path):
    out = tmp_path / "clip_cuts.txt"
    run_audio_silence(out, get_effective_config(None, {}))
    assert out.exists()
    assert out.read_text(encoding="utf-8").lstrip().startswith("#")
    assert read_cuts(out) == []


def test_with_raw_writes_cut_ranges(tmp_path):
    raw = tmp_path / "sd.log"
    raw.write_text(_RAW, encoding="utf-8")
    out = tmp_path / "clip_cuts.txt"
    run_audio_silence(out, get_effective_config(None, {}), raw_path=raw)
    assert read_cuts(out) == [(2.5, 4.0), (18.0, 20.0)]


def test_disabled_writes_header_only_even_with_raw(tmp_path):
    raw = tmp_path / "sd.log"
    raw.write_text(_RAW, encoding="utf-8")
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(yaml.safe_dump({"audio_silence": {"enabled": False}}), encoding="utf-8")
    out = tmp_path / "clip_cuts.txt"
    cfg = get_effective_config(cfg_file, {})
    run_audio_silence(out, cfg, raw_path=raw)
    assert read_cuts(out) == []
