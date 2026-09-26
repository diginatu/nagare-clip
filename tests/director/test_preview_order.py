"""The edit state under a playback order the model chose.

The op blocks stay in the transcript's order (their numbers are its numbers);
the order adds one block: the runs range by range as they play, with every
seam quoted, so the model reads the video it made rather than reassembling it.
"""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.director.preview import REORDERED_HEADER, edit_state, segment_preview

from .test_loop_order import A, B, _view

ORDER = [(4, 6), (1, 3), (7, 9)]


def _state(order=(), ops=None):
    return edit_state(_view(), [A, B], ops or {}, order=order)


class TestNoReorder:
    def test_shooting_order_adds_nothing(self):
        assert _state() == _state(order=[(1, 9)])
        assert REORDERED_HEADER not in _state(order=[(1, 3), (4, 9)])


class TestReordered:
    def test_it_lists_the_order_and_every_range(self):
        text = _state(ORDER)
        block = text[text.index(REORDERED_HEADER) :]
        assert "order: 4-6, 1-3, 7-9" in block
        assert block.index("▶ lines 4-6") < block.index("▶ lines 1-3") < block.index("▶ lines 7-9")

    def test_every_seam_is_quoted(self):
        text = _state(ORDER)
        assert "seam 6 → 1: 「あ5" in text
        assert "seam 3 → 7: 「[silent" in text

    def test_a_run_never_crosses_a_break(self):
        view = _view()
        runs = segment_preview(view, [A, B], 1, [], frozenset({3})).runs
        for run in runs:
            assert not (min(run.lines) <= 3 < max(run.lines))

    def test_the_runtime_does_not_depend_on_the_order(self):
        ops = {1: [DirectorOp(type="cut", lines=(4, 4), note="")]}
        plain, moved = _state(ops=ops), _state(ORDER, ops=ops)
        tail = plain[plain.index("whole video so far") :]
        assert tail in moved
