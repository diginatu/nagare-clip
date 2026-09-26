"""The planning turn: the conversation opens with the model's own plan.

The plan is state like the order — replaced whole by any reply that sends
``"plan"`` — so a plan the ops drifted from can be corrected by the one who
wrote both.  The view is ``test_loop_order``'s (9 display lines, two sources).
"""

from __future__ import annotations

import json

from nagare_clip.director.loop import (
    PLAN_REQUEST,
    PLAN_SUMMARY,
    LoopState,
    apply_reply,
    next_request,
    request_summary,
)
from nagare_clip.director.preview import PLAN_HEADER, STATE_HEADER, edit_state

from .test_loop_order import A, B, _view


def _cut(first, last):
    return {"type": "cut", "lines": [first, last], "note": "x"}


class TestTheFirstTurn:
    def test_it_asks_for_the_plan(self):
        assert next_request(_view(), LoopState(), 4) == PLAN_REQUEST
        assert request_summary(_view(), LoopState(), 4) == PLAN_SUMMARY

    def test_a_plan_sets_the_state_and_the_next_ask_is_a_range(self):
        view, state = _view(), LoopState()
        result = apply_reply(view, state, json.dumps({"plan": "  筋はこう  "}))
        assert result.error is None and result.refusal is None
        assert state.plan == "筋はこう"
        assert state.reviewed_through == 0
        assert "around lines 1 to 4" in next_request(view, state, 4)

    def test_it_may_carry_the_order(self):
        view, state = _view(), LoopState()
        result = apply_reply(view, state, json.dumps({"plan": "p", "order": [[7, 9], [1, 6]]}))
        assert result.reordered
        assert state.order == [(7, 9), (1, 6)]

    def test_a_reply_without_a_plan_is_unusable(self):
        view, state = _view(), LoopState()
        reply = json.dumps({"range": [1, 4], "reviewed_through": 4, "ops": [_cut(1, 1)]})
        result = apply_reply(view, state, reply)
        assert result.error is not None and "no plan yet" in result.error
        assert not state.ops and state.turns == 0

    def test_ops_without_a_range_are_unusable(self):
        result = apply_reply(_view(), LoopState(), json.dumps({"plan": "p", "ops": [_cut(1, 1)]}))
        assert result.error is not None

    def test_a_plan_must_be_text(self):
        for bad in ("", "   ", 3, ["p"]):
            result = apply_reply(_view(), LoopState(), json.dumps({"plan": bad}))
            assert result.error is not None
            assert "plan" in result.error


class TestThePlanIsState:
    def test_any_reply_may_replace_it(self):
        view, state = _view(), LoopState(plan="old")
        reply = json.dumps({"range": [1, 4], "reviewed_through": 4, "ops": [], "plan": "new"})
        apply_reply(view, state, reply)
        assert state.plan == "new"

    def test_alone_it_changes_nothing_else(self):
        view, state = _view(), LoopState(plan="old", reviewed_through=4)
        result = apply_reply(view, state, json.dumps({"plan": "new"}))
        assert result.error is None
        assert state.plan == "new" and state.reviewed_through == 4 and not state.ops

    def test_a_reply_without_it_keeps_it(self):
        view, state = _view(), LoopState(plan="kept")
        apply_reply(view, state, json.dumps({"range": [1, 4], "reviewed_through": 4, "ops": []}))
        assert state.plan == "kept"

    def test_an_unusable_reply_does_not_change_it(self):
        view, state = _view(), LoopState(plan="kept")
        apply_reply(view, state, json.dumps({"range": [9, 1], "plan": "lost"}))
        assert state.plan == "kept"


class TestTheEditState:
    def test_the_plan_leads_it(self):
        text = edit_state(_view(), [A, B], {}, plan="筋はこう")
        assert text.startswith(f"{STATE_HEADER}\n\n{PLAN_HEADER}\n筋はこう\n\n")

    def test_no_plan_no_block(self):
        assert PLAN_HEADER not in edit_state(_view(), [A, B], {})
