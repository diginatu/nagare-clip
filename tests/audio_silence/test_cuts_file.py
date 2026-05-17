"""Tests for the human-editable cut-list file format."""

import logging

import pytest

from nagare_clip.audio_silence.cuts_file import read_cuts, write_cuts


def test_write_read_round_trip(tmp_path):
    path = tmp_path / "clip_cuts.txt"
    ranges = [(12.3, 14.85), (1.0, 2.5)]
    write_cuts(path, ranges)

    # Written sorted by start, with a comment header present.
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#")

    assert read_cuts(path) == [
        pytest.approx((1.0, 2.5)),
        pytest.approx((12.3, 14.85)),
    ]


def test_write_empty_produces_header_only(tmp_path):
    path = tmp_path / "clip_cuts.txt"
    write_cuts(path, [])
    text = path.read_text(encoding="utf-8")
    assert text  # not empty
    assert all(line.startswith("#") for line in text.splitlines() if line.strip())
    assert read_cuts(path) == []


def test_read_skips_comments_and_blank_lines(tmp_path):
    path = tmp_path / "c.txt"
    path.write_text(
        "# header comment\n"
        "\n"
        "   \n"
        "3.000 - 6.000\n"
        "# trailing note\n",
        encoding="utf-8",
    )
    assert read_cuts(path) == [pytest.approx((3.0, 6.0))]


def test_read_skips_malformed_lines_with_warning(tmp_path, caplog):
    path = tmp_path / "c.txt"
    path.write_text(
        "not a range\n"
        "1.0 - 2.0\n"
        "5.0 - 3.0\n"  # start >= end, invalid
        "10 - 20\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        result = read_cuts(path)
    assert result == [pytest.approx((1.0, 2.0)), pytest.approx((10.0, 20.0))]
    assert len(caplog.records) >= 2


def test_read_missing_file_returns_empty(tmp_path):
    assert read_cuts(tmp_path / "does_not_exist.txt") == []
