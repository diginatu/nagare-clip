"""The prompt's job: make a reorder EXPRESSIBLE and state the contract.

Not to argue for one. The editorial call lives in the project brief, and
everything added to these prompts competes with it — this project has watched
the brief lose that competition repeatedly.
"""

from __future__ import annotations

import json
import re

from nagare_clip.config import PLAN_PROMPT, PLAN_REVISE_PROMPT
from nagare_clip.order import segments_from_dict


def _example_order(prompt: str):
    """The `order` array the prompt shows, parsed the way the code parses it."""
    match = re.search(r'"order":\s*(\[.*?\}\s*\])', prompt, re.DOTALL)
    assert match, "the prompt shows no order example"
    return segments_from_dict(json.loads(match.group(1)))


class TestPlanPrompt:
    def test_the_order_example_parses(self):
        assert _example_order(PLAN_PROMPT)

    def test_the_order_example_is_not_itself_a_reorder(self):
        # A worked reorder in this prompt would anchor harder than the brief
        # that is supposed to decide it.
        order = _example_order(PLAN_PROMPT)
        seen: list[str] = []
        for segment in order:
            if segment.stem not in seen:
                seen.append(segment.stem)
            else:
                assert segment.stem == order[order.index(segment) - 1].stem, (
                    "the example returns to an earlier source — that is a reorder"
                )
        last_by_stem: dict[str, int] = {}
        for segment in order:
            if segment.lines is not None:
                assert segment.lines[0] > last_by_stem.get(segment.stem, 0)
                last_by_stem[segment.stem] = segment.lines[1]

    def test_the_example_shows_that_lines_may_be_omitted(self):
        assert any(s.lines is None for s in _example_order(PLAN_PROMPT))

    def test_the_coverage_contract_is_stated(self):
        assert "exactly once" in PLAN_PROMPT.lower()

    def test_omitting_the_order_is_stated_to_mean_the_given_order(self):
        assert re.search(r'[Oo]mit .*"order"', PLAN_PROMPT)

    def test_a_reorder_must_be_announced_in_the_message(self):
        assert re.search(r'order.*"message"|"message".*order', PLAN_PROMPT, re.DOTALL)

    def test_it_speaks_of_segments(self):
        assert "segment" in PLAN_PROMPT.lower()


class TestPlanRevisePrompt:
    def test_the_order_example_parses(self):
        assert _example_order(PLAN_REVISE_PROMPT)

    def test_the_order_is_restated_whole_not_edited(self):
        # Order IS position, so a partial edit would need an insertion point --
        # exactly what improvement 23 removed from the direction ops.
        assert "whole" in PLAN_REVISE_PROMPT or "all of" in PLAN_REVISE_PROMPT

    def test_omitting_the_order_is_stated_to_keep_the_current_one(self):
        assert re.search(r'[Oo]mit .*"order"', PLAN_REVISE_PROMPT)

    def test_the_coverage_contract_is_stated(self):
        assert "exactly once" in PLAN_REVISE_PROMPT.lower()
