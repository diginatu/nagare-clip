"""The turn protocol: what to ask for next, and what a reply does to the state.

The view is :mod:`tests.director.test_display`'s — ten display lines over three
segments, one source playing twice — so a join sits after display line 4 and
after display line 7.
"""

from __future__ import annotations

import json

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.director.display import build_display_view
from nagare_clip.director.loop import LoopState, apply_reply, next_request, request_summary

from .test_display import SEGMENTS


def _view():
    return build_display_view(SEGMENTS)


def _reply(range_, reviewed_through, ops):
    return json.dumps({"range": list(range_), "reviewed_through": reviewed_through, "ops": ops})


def _cut(lines, note="x"):
    return {"type": "cut", "lines": list(lines), "note": note}


class TestNextRequest:
    def test_it_asks_for_an_approximate_range(self):
        view, state = _view(), LoopState(plan="p")
        message = next_request(view, state, 4)
        # "around" is the whole point: a hard boundary is what the plan stage's
        # line ranges did, and the director copied them as op boundaries.
        assert "around lines 1 to 4" in message
        assert "10" in message  # how much video there is in total

    def test_it_continues_after_the_line_the_model_reviewed_through(self):
        view, state = _view(), LoopState(plan="p")
        # The model reviewed PAST the range it was asked for.
        apply_reply(view, state, _reply((1, 4), 6, []))
        assert "around lines 7 to 10" in next_request(view, state, 4)

    def test_the_last_chunk_stops_at_the_last_line(self):
        assert "around lines 1 to 10" in next_request(_view(), LoopState(plan="p"), 40)

    def test_once_everything_is_reviewed_it_asks_to_finish(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply((1, 10), 10, []))
        message = next_request(view, state, 4)
        assert "around lines" not in message
        assert '"done"' in message


class TestTheHistoryLine:
    """What an earlier turn's ask is trimmed to once its state is stale.

    The conversation keeps its replies, so each one needs the ask it answered —
    but only enough of it to read the reply against: the range. The
    approximation wording and the reply shape are in the system prompt and in
    the LIVE request; repeated through the history they are noise re-sent
    uncached on every turn.
    """

    def test_it_is_one_short_line_naming_the_range(self):
        assert request_summary(_view(), LoopState(plan="p"), 4) == "Review around lines 1 to 4."

    def test_it_names_the_same_range_the_live_request_asks_for(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply((1, 6), 6, []))
        assert "around lines 7 to 10" in next_request(view, state, 4)
        assert request_summary(view, state, 4) == "Review around lines 7 to 10."

    def test_it_drops_the_wording_the_live_request_carries(self):
        line = request_summary(_view(), LoopState(plan="p"), 4)
        assert "approximately" not in line
        assert "JSON" not in line
        assert "\n" not in line

    def test_once_everything_is_reviewed_it_says_that_instead(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply((1, 10), 10, []))
        assert request_summary(view, state, 4) == "Every line has been reviewed."


class TestFirstPass:
    def test_an_op_is_stored_in_its_segment_in_source_coordinates(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(view, state, _reply((1, 4), 4, [_cut((1, 2), "うるさい")]))
        assert result.error is None and result.refusal is None
        assert result.ops == [DirectorOp(type="cut", lines=(4, 5), note="うるさい")]
        assert state.ops == {1: [DirectorOp(type="cut", lines=(4, 5), note="うるさい")]}
        assert (state.reviewed_through, state.turns) == (4, 1)

    def test_a_silence_line_at_an_edge_becomes_the_silence_reference(self):
        view, state = _view(), LoopState(plan="p")
        # Display 3 is the wait after source line 5 of segment 1.
        apply_reply(
            view,
            state,
            _reply((1, 4), 4, [{"type": "timelapse", "lines": [3, 3], "factor": 5.0}]),
        )
        assert state.ops[1] == [
            DirectorOp(type="timelapse", lines=(5, 5), factor=5.0, gap_start=True, gap_end=True)
        ]

    def test_ops_of_one_segment_stay_sorted_by_line(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply((1, 4), 4, [_cut((4, 4)), _cut((1, 1))]))
        assert [op.lines for op in state.ops[1]] == [(4, 4), (6, 6)]


class TestRewrite:
    def _pass(self, view, state):
        apply_reply(view, state, _reply((1, 4), 4, [_cut((1, 2), "first"), _cut((4, 4), "second")]))
        apply_reply(view, state, _reply((5, 7), 7, [_cut((5, 5), "third")]))
        return state

    def test_a_reply_replaces_every_op_starting_in_its_range(self):
        view, state = _view(), LoopState(plan="p")
        self._pass(view, state)
        apply_reply(view, state, _reply((1, 4), 7, [_cut((2, 2), "rewritten")]))
        assert [op.note for op in state.ops[1]] == ["rewritten"]
        assert [op.lines for op in state.ops[1]] == [(5, 5)]

    def test_it_disturbs_nothing_outside_its_range(self):
        view, state = _view(), LoopState(plan="p")
        self._pass(view, state)
        apply_reply(view, state, _reply((1, 4), 7, []))
        assert [op.note for op in state.ops[2]] == ["third"]

    def test_an_op_starting_outside_the_range_survives_it(self):
        view, state = _view(), LoopState(plan="p")
        # The op runs over display 3-4; a reply owning 1-2 does not own it,
        # although the two ranges are adjacent.
        apply_reply(view, state, _reply((1, 4), 4, [_cut((3, 4), "keep me")]))
        apply_reply(view, state, _reply((1, 2), 4, []))
        assert [op.note for op in state.ops[1]] == ["keep me"]

    def test_the_START_decides_even_when_the_op_reaches_past_the_range(self):
        view, state = _view(), LoopState(plan="p")
        # Display 1-3 starts inside a reply owning 1-2 and ends outside it:
        # it is the op's start that the range claims, not its overlap.
        apply_reply(view, state, _reply((1, 4), 4, [_cut((1, 3), "replace me")]))
        apply_reply(view, state, _reply((1, 2), 4, []))
        assert state.ops == {}

    def test_an_op_reaching_INTO_the_range_is_not_claimed_by_it(self):
        view, state = _view(), LoopState(plan="p")
        # Display 3-4 ends inside a reply owning 4-6 but starts before it.
        apply_reply(view, state, _reply((1, 4), 4, [_cut((3, 4), "keep me")]))
        apply_reply(view, state, _reply((4, 6), 6, []))
        assert [op.note for op in state.ops[1]] == ["keep me"]

    def test_a_rewrite_never_rewinds_the_progress(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply((1, 10), 10, []))
        apply_reply(view, state, _reply((1, 4), 4, [_cut((1, 1))]))
        assert state.reviewed_through == 10


class TestDone:
    def test_done_while_lines_remain_is_refused_by_name(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply((1, 4), 4, []))
        result = apply_reply(view, state, '{"done": true}')
        assert result.done is False
        assert result.refusal is not None and "5" in result.refusal
        # ...and the loop goes on from there.
        assert "around lines 5 to" in next_request(view, state, 4)

    def test_done_is_accepted_once_every_line_has_been_reviewed(self):
        view, state = _view(), LoopState(plan="p")
        apply_reply(view, state, _reply((1, 10), 10, []))
        result = apply_reply(view, state, '{"done": true}')
        assert (result.done, result.refusal) == (True, None)


class TestSegmentJoins:
    def test_a_join_crossing_cut_is_split_at_the_join(self):
        view, state = _view(), LoopState(plan="p")
        # Display 3-6 spans the join after display 4: the silence after source
        # line 5 of A through source line 1 of B and the silence after it.
        result = apply_reply(view, state, _reply((1, 7), 7, [_cut((3, 6), "long")]))
        assert result.refusal is None
        assert result.ops == [
            DirectorOp(type="cut", lines=(5, 6), note="long", gap_start=True),
            DirectorOp(type="cut", lines=(1, 1), note="long", gap_end=True),
        ]
        assert list(state.ops) == [1, 2]

    def test_a_join_crossing_keep_is_split_too(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(
            view, state, _reply((1, 7), 7, [{"type": "keep", "lines": [4, 5], "note": "k"}])
        )
        assert [(op.lines, op.gap_start, op.gap_end) for op in result.ops] == [
            ((6, 6), False, False),
            ((1, 1), False, False),
        ]

    def test_a_join_crossing_timelapse_is_refused_with_the_joins_line(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(
            view,
            state,
            _reply((1, 7), 7, [{"type": "timelapse", "lines": [3, 6], "factor": 5.0}]),
        )
        assert result.ops == [] and state.ops == {}
        assert result.refusal is not None
        assert "timelapse" in result.refusal and "4" in result.refusal

    def test_a_join_crossing_overlay_is_refused_as_well(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(
            view,
            state,
            _reply(
                (1, 7),
                7,
                [{"type": "overlay", "lines": [4, 5], "text": "配管", "duration": 2.0}],
            ),
        )
        assert result.ops == []
        assert result.refusal is not None and "overlay" in result.refusal

    def test_a_refused_op_does_not_take_the_others_with_it(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(
            view,
            state,
            _reply(
                (1, 7),
                7,
                [{"type": "timelapse", "lines": [3, 6], "factor": 5.0}, _cut((1, 1), "fine")],
            ),
        )
        assert [op.note for op in result.ops] == ["fine"]
        assert result.refusal is not None


class TestParserGuards:
    def test_a_timelapse_under_the_factor_floor_is_dropped_by_the_parser(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(
            view, state, _reply((1, 4), 4, [{"type": "timelapse", "lines": [1, 2], "factor": 2.0}])
        )
        assert result.ops == [] and state.ops == {}
        assert any("timelapse factor" in drop for drop in result.drops)

    def test_an_off_menu_type_is_dropped_by_the_parser(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(
            view, state, _reply((1, 4), 4, [{"type": "speed", "lines": [1, 2], "factor": 1.5}])
        )
        assert result.ops == []
        assert any("speed" in drop for drop in result.drops)

    def test_the_keep_width_cap_applies_to_display_lines(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(
            view,
            state,
            _reply((1, 4), 4, [{"type": "keep", "lines": [1, 4]}]),
            max_keep_lines=2,
        )
        assert result.ops == []
        assert any("max_keep_lines" in drop for drop in result.drops)

    def test_a_line_outside_the_video_is_dropped(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(view, state, _reply((1, 4), 4, [_cut((9, 11))]))
        assert result.ops == []
        assert any("bad lines" in drop for drop in result.drops)

    def test_the_old_silence_form_is_refused_not_reinterpreted(self):
        # "2~" addressed the silence after SOURCE line 2; here a silence is a
        # numbered line of its own, so the form would mean something else.
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(view, state, _reply((1, 4), 4, [_cut(["2~", 4])]))
        assert result.ops == [] and state.ops == {}
        assert any("~" in drop for drop in result.drops)


class TestMalformedReply:
    def test_it_comes_back_as_an_error_not_an_exception(self):
        view, state = _view(), LoopState(plan="p")
        result = apply_reply(view, state, "sure! here are the ops:")
        assert result.error is not None
        assert (result.ops, result.done, state.turns) == ([], False, 0)

    def test_a_reply_that_is_not_an_object(self):
        assert apply_reply(_view(), LoopState(plan="p"), "[1, 2]").error is not None

    def test_a_reply_with_neither_done_nor_range(self):
        assert apply_reply(_view(), LoopState(plan="p"), '{"ops": []}').error is not None

    def test_a_range_outside_the_video(self):
        assert apply_reply(_view(), LoopState(plan="p"), _reply((1, 99), 4, [])).error is not None

    def test_a_range_that_runs_backwards(self):
        assert apply_reply(_view(), LoopState(plan="p"), _reply((4, 1), 4, [])).error is not None

    def test_a_missing_reviewed_through(self):
        reply = json.dumps({"range": [1, 4], "ops": []})
        assert apply_reply(_view(), LoopState(plan="p"), reply).error is not None

    def test_ops_that_are_not_a_list(self):
        reply = json.dumps({"range": [1, 4], "reviewed_through": 4, "ops": "none"})
        assert apply_reply(_view(), LoopState(plan="p"), reply).error is not None

    def test_a_fenced_reply_is_still_read(self):
        view, state = _view(), LoopState(plan="p")
        fenced = "```json\n" + _reply((1, 4), 4, [_cut((1, 1))]) + "\n```"
        result = apply_reply(view, state, fenced)
        assert result.error is None and [op.lines for op in result.ops] == [(4, 4)]
