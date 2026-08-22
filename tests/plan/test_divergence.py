"""plan/director divergence: where the ops that landed contradict the plan.

Deterministic, no LLM call.  It never overrides the director — a divergence is
a signal to read, printed in the LLM report.
"""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.plan.divergence import find_divergences, format_divergences
from nagare_clip.plan.plan_llm import PartDirection


def _cut(a, b, note="drags"):
    return DirectorOp(type="cut", lines=(a, b), note=note)


class TestCutOverFeature:
    def test_reports_a_featured_range_mostly_cut(self):
        found = find_divergences(
            [PartDirection("v", (1, 10), "feature — the payoff")],
            {"v": [_cut(1, 8, "long digression")]},
        )
        assert len(found) == 1
        assert found[0].kind == "cut-over-feature"
        assert found[0].stem == "v"
        assert "0.8" in found[0].detail  # the share
        assert "0.5" in found[0].detail  # the threshold it is arguable against
        assert "long digression" in found[0].notes[0]

    def test_small_cut_share_is_not_a_divergence(self):
        assert (
            find_divergences(
                [PartDirection("v", (1, 10), "feature — the payoff")],
                {"v": [_cut(1, 2)]},
            )
            == []
        )

    def test_threshold_is_configurable(self):
        found = find_divergences(
            [PartDirection("v", (1, 10), "feature — the payoff")],
            {"v": [_cut(1, 2)]},
            cut_share=0.15,
        )
        assert len(found) == 1

    def test_only_the_overlapping_lines_count(self):
        # a cut entirely outside the directed range is not this part's business
        assert (
            find_divergences(
                [PartDirection("v", (1, 10), "feature — the payoff")],
                {"v": [_cut(20, 40)]},
            )
            == []
        )

    def test_other_video_ops_ignored(self):
        assert (
            find_divergences(
                [PartDirection("v", (1, 10), "feature — the payoff")],
                {"other": [_cut(1, 10)]},
            )
            == []
        )


class TestProtectedOverRemove:
    def test_reports_a_removed_range_that_got_protected(self):
        found = find_divergences(
            [PartDirection("v", (1, 10), "remove — repeats part 1")],
            {"v": [DirectorOp(type="keep", lines=(3, 4), note="the spill")]},
        )
        assert len(found) == 1
        assert found[0].kind == "protected-over-remove"
        assert "keep" in found[0].detail
        assert "the spill" in found[0].notes[0]

    def test_a_protecting_op_outside_the_range_is_not_this_part_s_business(self):
        assert (
            find_divergences(
                [PartDirection("v", (1, 10), "remove — repeats part 1")],
                {"v": [DirectorOp(type="keep", lines=(20, 40))]},
            )
            == []
        )

    def test_a_cut_op_over_a_remove_direction_agrees(self):
        assert (
            find_divergences(
                [PartDirection("v", (1, 10), "remove — repeats part 1")],
                {"v": [_cut(1, 10)]},
            )
            == []
        )


class TestTimelapseAsked:
    def test_reports_a_timelapse_direction_with_no_timelapse_op(self):
        found = find_divergences(
            [PartDirection("v", (1, 10), "timelapse — repetitive assembly")],
            {"v": []},
        )
        assert len(found) == 1
        assert found[0].kind == "no-timelapse"

    def test_satisfied_when_a_timelapse_op_lands(self):
        assert (
            find_divergences(
                [PartDirection("v", (1, 10), "timelapse — repetitive assembly")],
                {"v": [DirectorOp(type="timelapse", lines=(1, 10), factor=8.0)]},
            )
            == []
        )


class TestVocabulary:
    def test_keywords_are_read_from_the_leading_clause(self):
        # "remove" here describes what the part is about, not what to do with it
        assert (
            find_divergences(
                [PartDirection("v", (1, 10), "feature — he explains how to remove the pump")],
                {"v": [DirectorOp(type="keep", lines=(1, 2))]},
            )
            == []
        )

    def test_a_later_clause_does_not_override_the_verb(self):
        # "featured" here is part of the reason, not the instruction: the
        # direction still says remove, so a protecting op still diverges.
        found = find_divergences(
            [PartDirection("v", (1, 10), "remove — repeats the featured demo")],
            {"v": [DirectorOp(type="keep", lines=(3, 4))]},
        )
        assert [d.kind for d in found] == ["protected-over-remove"]

    def test_direction_with_no_known_verb_is_ignored(self):
        assert (
            find_divergences(
                [PartDirection("v", (1, 10), "the pump arrives")],
                {"v": [_cut(1, 10)]},
            )
            == []
        )


class TestFormat:
    def test_empty_renders_nothing(self):
        assert format_divergences([]) == ""

    def test_renders_a_readable_block(self):
        text = format_divergences(
            find_divergences(
                [PartDirection("v", (1, 10), "feature — the payoff")],
                {"v": [_cut(1, 8, "long digression")]},
            )
        )
        assert "plan/director divergence" in text.lower()
        assert "v [1-10]" in text
        assert "feature — the payoff" in text
        assert "long digression" in text
