"""publish/thumbs: which source moments are worth a still.

The director already marked the payoffs -- overlay captions, keeps rescuing a
silent event, and the boundaries of a timelapse -- so the shortlist is derived
from its ops rather than guessed.
"""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.publish.thumbs import (
    ThumbCandidate,
    cap_candidates,
    frame_relpath,
    select_candidates,
)

# line 1: 0-10s, line 2: 10-20s, line 3: 20-30s, line 4: 30-100s
SEG_TIMES = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 100.0)]


def test_overlay_op_yields_the_midpoint_of_its_own_line():
    ops = [DirectorOp(type="overlay", lines=(2, 2), text="水浸し！", duration=3.0)]
    assert select_candidates("a", ops, SEG_TIMES) == [
        ThumbCandidate(stem="a", time=15.0, kind="overlay", label="水浸し！")
    ]


def test_overlay_op_anchors_on_the_first_line_of_a_range():
    ops = [DirectorOp(type="overlay", lines=(2, 4), text="呼び水、完成！", duration=3.0)]
    assert [c.time for c in select_candidates("a", ops, SEG_TIMES)] == [15.0]


def test_keep_op_yields_the_midpoint_of_the_whole_kept_event():
    ops = [DirectorOp(type="keep", lines=(1, 3), note="water spill cleanup")]
    assert select_candidates("a", ops, SEG_TIMES) == [
        ThumbCandidate(stem="a", time=15.0, kind="keep", label="water spill cleanup")
    ]


def test_timelapse_op_yields_both_boundaries():
    ops = [DirectorOp(type="timelapse", lines=(2, 4), factor=8.0, text="配管作業")]
    got = select_candidates("a", ops, SEG_TIMES)
    assert [(c.time, c.kind) for c in got] == [
        (10.0, "timelapse-start"),
        (100.0, "timelapse-end"),
    ]
    assert {c.label for c in got} == {"配管作業"}


def test_cut_speed_and_edit_ops_are_not_payoffs():
    ops = [
        DirectorOp(type="cut", lines=(1, 2)),
        DirectorOp(type="speed", lines=(2, 3), factor=1.5),
        DirectorOp(type="edit", lines=(3, 3), note="fix a word"),
    ]
    assert select_candidates("a", ops, SEG_TIMES) == []


def test_ops_outside_the_transcript_are_skipped():
    ops = [DirectorOp(type="overlay", lines=(9, 9), text="ghost", duration=2.0)]
    assert select_candidates("a", ops, SEG_TIMES) == []


def test_untimed_lines_are_skipped():
    ops = [DirectorOp(type="overlay", lines=(1, 1), text="x", duration=2.0)]
    assert select_candidates("a", ops, [(None, None)]) == []


def test_candidates_come_back_in_time_order():
    ops = [
        DirectorOp(type="overlay", lines=(3, 3), text="late", duration=2.0),
        DirectorOp(type="keep", lines=(1, 1), note="early"),
    ]
    assert [c.time for c in select_candidates("a", ops, SEG_TIMES)] == [5.0, 25.0]


def test_duplicate_moments_are_collapsed():
    """A keep and an overlay on the same line describe one moment, and the
    frame would be extracted twice under the same name."""
    ops = [
        DirectorOp(type="keep", lines=(2, 2), note="spill"),
        DirectorOp(type="overlay", lines=(2, 2), text="水浸し！", duration=3.0),
    ]
    got = select_candidates("a", ops, SEG_TIMES)
    assert [c.time for c in got] == [15.0]
    # the overlay's own words are the better label to show a human
    assert got[0].label == "水浸し！"


def test_cap_keeps_overlays_before_timelapses_before_keeps():
    cands = [
        ThumbCandidate("a", 1.0, "keep", "k1"),
        ThumbCandidate("a", 2.0, "timelapse-start", "t1"),
        ThumbCandidate("a", 3.0, "overlay", "o1"),
        ThumbCandidate("b", 4.0, "keep", "k2"),
    ]
    got = cap_candidates(cands, 2)
    assert [c.label for c in got] == ["t1", "o1"]  # still in (stem, time) order


def test_cap_of_zero_or_more_than_available_keeps_everything():
    cands = [ThumbCandidate("a", 1.0, "keep", "k")]
    assert cap_candidates(cands, 0) == cands
    assert cap_candidates(cands, 5) == cands


def test_frame_relpath_is_stable_and_stem_scoped():
    assert frame_relpath("talk1", 15.0) == "frames/talk1/15.000.jpg"
    assert frame_relpath("talk1", 15.0004) == frame_relpath("talk1", 15.0)
