"""Tests for the audio_silence Stage 2 CLI."""

import sys

import yaml

from nagare_clip.audio_silence import cli as audio_cli
from nagare_clip.audio_silence.cuts_file import read_cuts

_RAW = """\
    Duration: 00:00:20.00, start: 0.000000
[silencedetect @ 0x55] silence_start: 2.5
[silencedetect @ 0x55] silence_end: 4.0 | silence_duration: 1.5
[silencedetect @ 0x55] silence_start: 18.0
"""


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["audio_silence.cli", *argv])
    audio_cli.main()


def test_cli_without_raw_writes_header_only(tmp_path, monkeypatch):
    out = tmp_path / "clip_cuts.txt"
    _run(monkeypatch, ["--output", str(out)])
    assert out.exists()
    assert out.read_text(encoding="utf-8").lstrip().startswith("#")
    assert read_cuts(out) == []


def test_cli_with_raw_writes_cut_ranges(tmp_path, monkeypatch):
    raw = tmp_path / "sd.log"
    raw.write_text(_RAW, encoding="utf-8")
    out = tmp_path / "clip_cuts.txt"
    _run(monkeypatch, ["--raw", str(raw), "--output", str(out)])
    assert read_cuts(out) == [(2.5, 4.0), (18.0, 20.0)]


def test_cli_disabled_writes_header_only_even_with_raw(tmp_path, monkeypatch):
    raw = tmp_path / "sd.log"
    raw.write_text(_RAW, encoding="utf-8")
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        yaml.safe_dump({"audio_silence": {"enabled": False}}), encoding="utf-8"
    )
    out = tmp_path / "clip_cuts.txt"
    _run(
        monkeypatch,
        ["--raw", str(raw), "--output", str(out), "--config", str(cfg)],
    )
    assert read_cuts(out) == []
