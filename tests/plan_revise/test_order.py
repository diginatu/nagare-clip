"""plan_revise sees the current order and restates it whole.

Restating something whole requires seeing it whole: without the render, the
first turn asking for a reorder would have the model invent one from nothing.
"""

from __future__ import annotations

import json

from nagare_clip.order import Segment
from nagare_clip.plan.dialogue import DialogueTurn
from nagare_clip.plan.plan_llm import PartDirection, format_order
from nagare_clip.plan_revise.revise_llm import (
    apply_revision,
    build_user_content,
    generate_revision,
    try_parse_revision,
)
from nagare_clip.summary.summarize import PartSummary, ProjectSummary

PARTS = [
    PartSummary("mix", (1, 30), "餌やり"),
    PartSummary("mix", (31, 97), "装置と魚"),
    PartSummary("dev", (1, 40), "装置づくり"),
]
COUNTS = {"mix": 97, "dev": 40}
SHOOTING = [Segment("dev", None), Segment("mix", None)]
REORDER = [Segment("dev", None), Segment("mix", (31, 97)), Segment("mix", (1, 30))]


class TestRenderingTheCurrentOrder:
    def test_the_order_is_rendered_in_playback_order(self):
        assert format_order(REORDER).splitlines()[1:] == [
            "1. dev",
            "2. mix [31-97]",
            "3. mix [1-30]",
        ]

    def test_an_identity_order_renders_as_identity_not_as_nothing(self):
        # "no reorder yet" must be a visible state, not an absence.
        rendered = format_order(SHOOTING)
        assert "1. dev" in rendered and "2. mix" in rendered

    def test_it_reaches_the_user_content(self):
        content = build_user_content(ProjectSummary("全体", PARTS), [], [], [], order=REORDER)
        assert "2. mix [31-97]" in content

    def test_no_order_renders_no_block(self):
        content = build_user_content(ProjectSummary("全体", PARTS), [], [], [])
        assert "playback order" not in content.lower()

    def test_the_conversation_still_follows_the_order(self):
        content = build_user_content(
            ProjectSummary("全体", PARTS),
            [],
            [],
            [DialogueTurn("human", "装置を先に")],
            order=REORDER,
        )
        assert content.index("mix [31-97]") < content.index("装置を先に")


class TestParsingTheOrder:
    def _ops(self, payload, counts=COUNTS):
        return try_parse_revision(json.dumps(payload), PARTS, line_counts=counts)

    def test_a_restated_order_is_parsed(self):
        ops = self._ops({"order": [{"stem": "dev"}, {"stem": "mix"}]})
        assert ops.order == SHOOTING

    def test_an_invalid_order_is_dropped_whole(self):
        drops = []
        ops = try_parse_revision(
            json.dumps({"order": [{"stem": "dev"}]}), PARTS, drops, line_counts=COUNTS
        )
        assert ops.order is None
        assert drops

    def test_no_order_key_means_keep_the_current_one(self):
        assert self._ops({"message": "ok"}).order is None

    def test_an_order_alone_is_a_valid_revision(self):
        assert self._ops({"order": [{"stem": "dev"}, {"stem": "mix"}]}) is not None


class TestApplyingTheOrder:
    def test_a_restated_order_replaces_the_current_one(self):
        ops = try_parse_revision(
            json.dumps({"order": [{"stem": "mix"}, {"stem": "dev"}]}),
            PARTS,
            line_counts=COUNTS,
        )
        revision = apply_revision([], ops, PARTS, current_order=REORDER)
        assert revision.order == [Segment("mix", None), Segment("dev", None)]

    def test_an_omitted_order_is_inherited_not_cleared(self):
        # The same rule that governs directions: what no operation names
        # survives by the code, not by the model's diligence.
        ops = try_parse_revision(json.dumps({"message": "ok"}), PARTS, line_counts=COUNTS)
        revision = apply_revision([], ops, PARTS, current_order=REORDER)
        assert revision.order == REORDER

    def test_an_invalid_order_leaves_the_current_one_standing(self):
        ops = try_parse_revision(
            json.dumps({"order": [{"stem": "dev"}]}), PARTS, line_counts=COUNTS
        )
        revision = apply_revision([], ops, PARTS, current_order=REORDER)
        assert revision.order == REORDER


class TestGenerateRevision:
    def test_the_order_reaches_the_revision(self):
        result = generate_revision(
            ProjectSummary("全体", PARTS),
            [PartDirection("dev", (1, 40), "feature")],
            [DialogueTurn("human", "装置を先に")],
            {"prompt": "P", "max_retries": 0},
            call_llm=lambda m, c: json.dumps(
                {"order": [{"stem": "mix"}, {"stem": "dev"}], "message": "並べ替えました"}
            ),
            order=SHOOTING,
            line_counts=COUNTS,
        )
        assert result.ok
        assert result.order == [Segment("mix", None), Segment("dev", None)]

    def test_a_failed_call_keeps_the_current_order(self):
        def boom(messages, cfg):
            raise RuntimeError("nope")

        result = generate_revision(
            ProjectSummary("全体", PARTS),
            [],
            [DialogueTurn("human", "x")],
            {"prompt": "P", "max_retries": 0},
            call_llm=boom,
            order=REORDER,
            line_counts=COUNTS,
        )
        assert not result.ok
        assert result.order == REORDER
