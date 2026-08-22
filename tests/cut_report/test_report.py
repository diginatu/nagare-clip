"""The markdown block inlined into llm_report/index.md.

Two kinds of content, and the distinction matters: the measurements print
whether or not anything is wrong (they are the regression table), the findings
only when a stated threshold is breached.
"""

from __future__ import annotations

from nagare_clip.config import get_effective_config
from nagare_clip.cut_report.checks import find_issues
from nagare_clip.cut_report.metrics import measure
from nagare_clip.cut_report.report import build_cut_report, format_cut_report

from .conftest import intervals


def _cfg(**over):
    cfg = get_effective_config(None)
    cfg["cut_report"].update(over)
    return cfg


class TestMeasurementsAlwaysPrint:
    def test_a_clean_run_still_reports_every_number(self):
        srcs = [intervals("one", 600.0, [(0.0, 300.0)], captions=[(0.0, 10.0, "字幕")])]
        text = build_cut_report(srcs, _cfg())
        assert "## finished cut" in text
        assert "source" in text and "10.0 min" in text
        assert "5.0 min" in text  # finished
        assert "keep intervals" in text
        assert "captions" in text
        assert "overlays" in text
        assert "strips" in text

    def test_a_clean_run_says_so_instead_of_listing_findings(self):
        srcs = [intervals("one", 600.0, [(0.0, 300.0)])]
        text = build_cut_report(srcs, _cfg())
        assert "within threshold" in text

    def test_keep_health_is_one_summary_line_not_one_line_per_gap(self):
        # 40 healthy gaps must not become 40 lines.
        keeps = [(t, t + 5.0) for t in range(0, 400, 10)]
        srcs = [intervals("one", 600.0, keeps)]
        text = build_cut_report(srcs, _cfg())
        assert text.count("keep gaps") == 1
        assert "min 5.00s, median 5.00s" in text
        # The verdict, not the 39 healthy gaps behind it.
        assert "below intervals.min_cut (0.40s): 0" in text
        assert len(text.splitlines()) < 30

    def test_the_gap_threshold_comes_from_the_intervals_stages_own_min_cut(self):
        # intervals.min_cut is the pass that owns inter-keep gaps, so it is the
        # number the report must argue against rather than one of its own.
        srcs = [intervals("one", 600.0, [(0.0, 10.0), (10.8, 300.0)])]
        cfg = _cfg()
        cfg["intervals"]["min_cut"] = 1.5
        text = build_cut_report(srcs, cfg)
        assert "below intervals.min_cut (1.50s): 1" in text
        assert "keep-gap" in text


class TestFindingsPrintTheirThreshold:
    def test_the_breaches_are_named_with_their_numbers(self, two_sources):
        text = build_cut_report(two_sources, _cfg())
        assert "caption-compressed" in text
        assert "timelapse-short" in text
        assert "8.0" in text
        assert "はやい" in text

    def test_config_thresholds_reach_the_checks(self, two_sources):
        text = build_cut_report(
            two_sources, _cfg(caption_chars_per_sec=99.0, timelapse_min_screen=1.0)
        )
        assert "caption-compressed" not in text
        assert "timelapse-short" not in text

    def test_blender_warnings_are_surfaced(self, two_sources):
        text = build_cut_report(two_sources, _cfg(), blender_warnings=["Strip 109: clamped"])
        assert "Strip 109: clamped" in text


class TestDisabled:
    def test_disabled_writes_nothing(self, two_sources):
        assert build_cut_report(two_sources, _cfg(enabled=False)) == ""


class TestFormatting:
    def test_the_block_is_a_heading_and_ends_with_a_newline(self, two_sources):
        text = format_cut_report(
            measure(two_sources), find_issues(two_sources, measure(two_sources))
        )
        assert text.startswith("## finished cut")
        assert text.endswith("\n")
