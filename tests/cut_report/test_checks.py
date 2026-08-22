"""Breaches of a stated threshold, and the thresholds staying quiet when nothing breaches.

Every finding reports the number it breached so the threshold is arguable
rather than hidden -- the same rule the plan/director divergence note follows.
"""

from __future__ import annotations

import pytest

from nagare_clip.cut_report.checks import find_issues
from nagare_clip.cut_report.metrics import measure

from .conftest import intervals


def _issues(sources, **kw):
    return find_issues(sources, measure(sources, **_metric_kw(kw)), **kw)


def _metric_kw(kw):
    return {k: v for k, v in kw.items() if k in ("gap_threshold", "fragment_threshold")}


class TestCaptionReadingSpeed:
    def test_a_dense_caption_inside_a_speed_range_names_the_factor(self, two_sources):
        found = [f for f in _issues(two_sources) if f.kind == "caption-compressed"]
        assert len(found) == 1
        f = found[0]
        assert f.stem == "one"
        assert "8.0" in f.detail  # the factor doing the compressing
        assert "18.0" in f.detail  # the threshold it breached
        assert "30.0 chars/s" in f.detail  # its authored rate
        # The compression is the defect: 1.00s of source becomes 0.12s on screen.
        assert "0.12s on screen" in f.detail
        assert "240.0 chars/s" in f.detail
        assert "はやい" in f.text

    def test_a_dense_caption_outside_any_speed_range_is_a_separate_kind(self):
        srcs = [intervals("one", 100.0, [(0.0, 100.0)], captions=[(0.0, 1.0, "あ" * 30)])]
        (f,) = [f for f in _issues(srcs) if f.kind.startswith("caption-")]
        assert f.kind == "caption-fast"
        assert "30.0 chars/s" in f.detail

    def test_ordinary_captions_inside_a_speed_range_are_not_each_flagged(self):
        # 148 readable captions rode inside one 8x range on the real run; a
        # check that flags all of them is the same as no check.
        srcs = [
            intervals(
                "one",
                100.0,
                [(0.0, 100.0)],
                captions=[(t, t + 2.0, "ふつうの 字幕") for t in range(0, 90, 3)],
                speed_ranges=[(0.0, 100.0, 8.0)],
            )
        ]
        assert [f for f in _issues(srcs) if f.kind.startswith("caption-")] == []

    def test_threshold_is_configurable(self, two_sources):
        assert [
            f for f in _issues(two_sources, caption_cps=40.0) if f.kind.startswith("caption-")
        ] == []

    def test_a_zero_length_caption_is_skipped_not_divided_by(self):
        srcs = [intervals("one", 10.0, [(0.0, 10.0)], captions=[(1.0, 1.0, "あ")])]
        assert [f for f in _issues(srcs) if f.kind.startswith("caption-")] == []


class TestTimelapseOnScreenDuration:
    """Only the LONG side is a flag; a short timelapse is a measurement.

    There is no floor. A 6x fast-forward running 15.5s is ordinary vlog
    grammar -- the viewer loses 93s of audio and nothing else -- so the
    "about a minute on screen" target exists to stop a factor picked too low
    from leaving a long fast-forward on screen, not to police the short side.
    """

    def test_a_short_timelapse_is_never_flagged(self, two_sources):
        # 80s of source at 8x = 10.0s on screen.
        assert [f for f in _issues(two_sources) if f.kind.startswith("timelapse-")] == []

    def test_the_real_runs_15_5s_timelapse_is_not_flagged(self):
        # PXL_20260426_090431216: 92.8s of kept footage at factor 6.0.
        srcs = [intervals("one", 600.0, [(409.1, 501.9)], speed_ranges=[(409.1, 501.9, 6.0)])]
        assert [f for f in _issues(srcs) if f.kind.startswith("timelapse-")] == []

    def test_a_timelapse_under_the_ceiling_is_quiet(self):
        srcs = [intervals("one", 1000.0, [(0.0, 480.0)], speed_ranges=[(0.0, 480.0, 8.0)])]
        assert [f for f in _issues(srcs) if f.kind.startswith("timelapse-")] == []

    def test_a_sustained_fast_forward_over_the_ceiling_is_flagged(self):
        # The failure mode the operator prompt names: a mild factor leaving a
        # large share of the finished video as fast-forward.
        srcs = [intervals("one", 3000.0, [(0.0, 2000.0)], speed_ranges=[(0.0, 2000.0, 4.0)])]
        (f,) = [f for f in _issues(srcs) if f.kind.startswith("timelapse-")]
        assert f.kind == "timelapse-long"
        assert "500.0s on screen" in f.detail
        assert "180.0s" in f.detail  # the threshold it breached

    def test_the_ceiling_is_configurable(self):
        srcs = [intervals("one", 3000.0, [(0.0, 2000.0)], speed_ranges=[(0.0, 2000.0, 4.0)])]
        assert [
            f for f in _issues(srcs, timelapse_max=600.0) if f.kind.startswith("timelapse-")
        ] == []

    def test_there_is_no_floor_to_configure(self):
        with pytest.raises(TypeError):
            _issues([], timelapse_min=30.0)


class TestKeepHealth:
    def test_a_healthy_run_reports_nothing(self, two_sources):
        assert [f for f in _issues(two_sources) if f.kind.startswith("keep-")] == []

    def test_a_gap_under_min_cut_names_both_numbers(self):
        srcs = [intervals("one", 100.0, [(0.0, 10.0), (10.1, 20.0)])]
        (f,) = [f for f in _issues(srcs, gap_threshold=0.4) if f.kind == "keep-gap"]
        assert "0.10s" in f.detail
        assert "0.40s" in f.detail

    def test_a_fragment_under_the_threshold_is_flagged(self):
        srcs = [intervals("one", 100.0, [(0.0, 0.5), (10.0, 20.0)])]
        (f,) = [f for f in _issues(srcs, fragment_threshold=1.0) if f.kind == "keep-fragment"]
        assert "0.50s" in f.detail


class TestBlenderWarnings:
    def test_captured_warnings_are_surfaced_as_findings(self, two_sources):
        found = _issues(two_sources, blender_warnings=["Strip 109: interval clamped"])
        (f,) = [f for f in found if f.kind == "blender-warning"]
        assert f.detail == "Strip 109: interval clamped"

    def test_no_warnings_is_no_finding(self, two_sources):
        assert [f for f in _issues(two_sources) if f.kind == "blender-warning"] == []


class TestOrdering:
    def test_findings_are_grouped_by_kind_in_severity_order(self, two_sources):
        kinds = [f.kind for f in _issues(two_sources, blender_warnings=["w"])]
        assert kinds == ["caption-compressed", "blender-warning"]
