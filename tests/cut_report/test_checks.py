"""Breaches of a stated threshold, and the thresholds staying quiet when nothing breaches.

Every finding reports the number it breached so the threshold is arguable
rather than hidden -- the same rule the plan/director divergence note follows.
"""

from __future__ import annotations

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
    def test_a_timelapse_over_before_it_registers_is_flagged(self, two_sources):
        (f,) = [f for f in _issues(two_sources) if f.kind == "timelapse-short"]
        assert f.stem == "one"
        assert "10.0s on screen" in f.detail
        assert "30.0s" in f.detail  # the threshold

    def test_a_timelapse_inside_the_band_is_quiet(self):
        srcs = [intervals("one", 1000.0, [(0.0, 480.0)], speed_ranges=[(0.0, 480.0, 8.0)])]
        assert [f for f in _issues(srcs) if f.kind.startswith("timelapse-")] == []

    def test_a_timelapse_far_over_a_minute_is_flagged_too(self):
        srcs = [intervals("one", 3000.0, [(0.0, 2000.0)], speed_ranges=[(0.0, 2000.0, 4.0)])]
        (f,) = [f for f in _issues(srcs) if f.kind.startswith("timelapse-")]
        assert f.kind == "timelapse-long"
        assert "500.0s on screen" in f.detail

    def test_band_is_configurable(self, two_sources):
        assert [
            f for f in _issues(two_sources, timelapse_min=5.0) if f.kind.startswith("timelapse-")
        ] == []


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
        assert kinds == ["caption-compressed", "timelapse-short", "blender-warning"]
