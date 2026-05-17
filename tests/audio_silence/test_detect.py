"""Tests for ffmpeg silencedetect parsing and arg building."""

import pytest

from nagare_clip.audio_silence.detect import (
    build_ffmpeg_args,
    parse_silencedetect_output,
)

# --- parse_silencedetect_output ---

_PAIRED_WITH_TRAILING = """\
ffmpeg version n8.1 Copyright (c) 2000-2026 the FFmpeg developers
  Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'sample.mp4':
    Duration: 00:00:20.00, start: 0.000000, bitrate: 1000 kb/s
  Stream #0:0(und): Audio: aac
[silencedetect @ 0x55] silence_start: 2.5
[silencedetect @ 0x55] silence_end: 4.0 | silence_duration: 1.5
[silencedetect @ 0x55] silence_start: 18.0
[out#0/null @ 0x66] video:0kB audio:0kB
"""

_NO_SILENCE = """\
  Duration: 00:00:09.00, start: 0.000000, bitrate: 500 kb/s
[out#0/null @ 0x66] video:0kB audio:0kB
"""

_HMS_DURATION_TRAILING = """\
    Duration: 00:01:49.50, start: 0.000000, bitrate: 1234 kb/s
[silencedetect @ 0x55] silence_start: 100.0
"""

_ORPHAN_END = """\
    Duration: 00:00:10.00, start: 0.000000
[silencedetect @ 0x55] silence_end: 3.0 | silence_duration: 1.0
[silencedetect @ 0x55] silence_start: 5.0
[silencedetect @ 0x55] silence_end: 6.5 | silence_duration: 1.5
"""


def test_parse_paired_and_trailing_open_silence():
    result = parse_silencedetect_output(_PAIRED_WITH_TRAILING)
    assert result == [(2.5, 4.0), (18.0, 20.0)]


def test_parse_no_silence_returns_empty():
    assert parse_silencedetect_output(_NO_SILENCE) == []


def test_parse_empty_input_returns_empty():
    assert parse_silencedetect_output("") == []


def test_parse_hms_duration_closes_trailing_silence():
    result = parse_silencedetect_output(_HMS_DURATION_TRAILING)
    assert result == [pytest.approx((100.0, 109.5))]


def test_parse_ignores_orphan_silence_end():
    result = parse_silencedetect_output(_ORPHAN_END)
    assert result == [(5.0, 6.5)]


def test_parse_ignores_unrelated_lines():
    noisy = (
        "random log line\n"
        "    Duration: 00:00:08.00, start: 0.0\n"
        "frame= 10 fps=0.0\n"
        "[silencedetect @ 0x1] silence_start: 1.0\n"
        "[silencedetect @ 0x1] silence_end: 2.0 | silence_duration: 1.0\n"
        "# this is not a real ffmpeg line\n"
    )
    assert parse_silencedetect_output(noisy) == [(1.0, 2.0)]


# --- build_ffmpeg_args ---


def test_build_ffmpeg_args():
    args = build_ffmpeg_args("sub dir/clip.mp4", -30.0, 0.8)
    assert args == [
        "-hide_banner",
        "-nostats",
        "-i",
        "sub dir/clip.mp4",
        "-af",
        "silencedetect=noise=-30.0dB:d=0.8",
        "-f",
        "null",
        "-",
    ]
