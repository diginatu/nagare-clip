"""Measurements of the finished cut — always printed, whether or not anything is wrong.

These are the regression table the operator prompt asks a human to keep by hand.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from nagare_clip.cut_report.metrics import measure

from .conftest import intervals


def test_import_does_not_require_bpy():
    """The report runs in the pipeline process, where bpy does not exist.

    It reuses the blender stage's speed-splitting helper and the blender
    stage's warning-file contract, so both must stay bpy-free -- guarded in a
    fresh interpreter because other tests stub ``sys.modules["bpy"]`` and would
    mask a real bpy import.
    """
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, nagare_clip.cut_report.report, nagare_clip.pipeline.stages; "
            "assert 'bpy' not in sys.modules",
        ],
        check=True,
    )


class TestFinishedShape:
    def test_durations_and_speed_split(self, two_sources):
        m = measure(two_sources)
        assert m.sources == 2
        assert m.source_duration == pytest.approx(200.0)
        # one: 10s at 1x + 80s at 8x (=10s), two: 40s at 1x
        assert m.finished_duration == pytest.approx(60.0)
        assert m.plain_duration == pytest.approx(50.0)
        assert m.sped_duration == pytest.approx(10.0)
        assert m.plain_share == pytest.approx(50.0 / 60.0)
        assert m.sped_share == pytest.approx(10.0 / 60.0)
        assert m.finished_share == pytest.approx(60.0 / 200.0)

    def test_counts(self, two_sources):
        m = measure(two_sources)
        assert m.keep_intervals == 3
        assert m.strips == 3
        assert m.captions == 3
        assert m.captions_in_speed == 1
        assert m.overlays == 1

    def test_strips_count_speed_split_pieces_not_keep_intervals(self):
        # A speed range covering only part of a keep interval splits it into
        # two strips -- which is why blender's strip count exceeds the keep count.
        srcs = [intervals("one", 100.0, [(0.0, 100.0)], speed_ranges=[(50.0, 100.0, 4.0)])]
        m = measure(srcs)
        assert m.keep_intervals == 1
        assert m.strips == 2

    def test_overlay_density_is_per_finished_minute(self, two_sources):
        m = measure(two_sources)
        assert m.overlay_density == pytest.approx(1.0)

    def test_empty_project_measures_zero_without_dividing(self):
        m = measure([])
        assert m.finished_duration == 0.0
        assert m.overlay_density == 0.0
        assert m.plain_share == 0.0
        assert m.finished_share == 0.0


class TestSpeedSpans:
    def test_kept_source_seconds_and_on_screen_time(self, two_sources):
        (span,) = measure(two_sources).speed_spans
        assert span.stem == "one"
        assert (span.start, span.end, span.factor) == (20.0, 100.0, 8.0)
        assert span.kept == pytest.approx(80.0)
        assert span.screen == pytest.approx(10.0)

    def test_only_kept_footage_counts_toward_on_screen_time(self):
        # Half the range was cut, so it plays for half as long as span/factor.
        srcs = [intervals("one", 100.0, [(0.0, 50.0)], speed_ranges=[(0.0, 100.0, 5.0)])]
        (span,) = measure(srcs).speed_spans
        assert span.kept == pytest.approx(50.0)
        assert span.screen == pytest.approx(10.0)


class TestKeepHealth:
    def test_gaps_are_within_a_source_never_across_the_seam(self, two_sources):
        # 3 keep intervals over 2 sources -> 1 gap, not 2.
        g = measure(two_sources).gaps
        assert g.count == 1
        assert g.minimum == pytest.approx(10.0)
        assert g.median == pytest.approx(10.0)
        assert g.below == 0

    def test_a_later_source_never_opens_a_gap_across_the_concatenation_seam(self):
        # Source two starts 100s after source one's last keep ends. Pooling
        # both sources' intervals would invent a 100s "cut" that is really the
        # boundary between two videos.
        srcs = [
            intervals("one", 200.0, [(0.0, 50.0), (60.0, 100.0)]),
            intervals("two", 300.0, [(200.0, 240.0)]),
        ]
        g = measure(srcs).gaps
        assert g.count == 1
        assert g.minimum == pytest.approx(10.0)

    def test_touching_intervals_do_not_count_as_a_zero_length_gap(self):
        # A zero-length gap would drag the minimum to 0.00s and be reported as
        # a cut below min_cut that nobody made.
        srcs = [intervals("one", 100.0, [(0.0, 10.0), (10.0, 20.0), (30.0, 40.0)])]
        g = measure(srcs, gap_threshold=0.4).gaps
        assert g.count == 1
        assert g.minimum == pytest.approx(10.0)
        assert g.below == 0

    def test_fragments_are_the_keep_intervals_themselves(self, two_sources):
        f = measure(two_sources).fragments
        assert f.count == 3
        assert f.minimum == pytest.approx(10.0)
        assert f.median == pytest.approx(40.0)
        assert f.below == 0

    def test_below_counts_use_the_given_thresholds(self):
        srcs = [intervals("one", 100.0, [(0.0, 0.5), (0.6, 10.0)])]
        m = measure(srcs, gap_threshold=0.4, fragment_threshold=1.0)
        assert m.gaps.below == 1  # the 0.1s gap
        assert m.fragments.below == 1  # the 0.5s fragment

    def test_stats_of_an_empty_set_do_not_raise(self):
        m = measure([intervals("one", 10.0, [])])
        assert m.gaps.count == 0
        assert m.gaps.minimum == 0.0
        assert m.gaps.median == 0.0
