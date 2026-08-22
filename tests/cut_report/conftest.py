"""Shared fixtures: a tiny two-source project shaped like a real run."""

from __future__ import annotations

import pytest


def intervals(
    stem,
    duration,
    keeps,
    captions=(),
    speed_ranges=(),
    overlays=(),
):
    return (
        stem,
        {
            "source_file": stem,
            "duration_sec": duration,
            "keep_intervals": [{"start": a, "end": b} for a, b in keeps],
            "captions": [{"start": a, "end": b, "text": t} for a, b, t in captions],
            "speed_ranges": [{"start": a, "end": b, "factor": f} for a, b, f in speed_ranges],
            "overlays": [{"start": a, "duration": d, "text": t} for a, d, t in overlays],
        },
    )


@pytest.fixture
def two_sources():
    """100s + 100s of source; one 8x timelapse over 80s of the first."""
    return [
        intervals(
            "one",
            100.0,
            [(0.0, 10.0), (20.0, 100.0)],
            captions=[(0.0, 10.0, "ゆっくり"), (20.0, 21.0, "はやい" * 10)],
            speed_ranges=[(20.0, 100.0, 8.0)],
            overlays=[(0.0, 4.0, "みだし")],
        ),
        intervals(
            "two",
            100.0,
            [(0.0, 40.0)],
            captions=[(0.0, 2.0, "ふつうの 字幕")],
        ),
    ]
