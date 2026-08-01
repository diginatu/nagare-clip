"""Thumbnail frame shortlist: the director's payoff moments, on kept footage."""

from __future__ import annotations

from nagare_clip.director.director_llm import DirectorOp
from nagare_clip.publish.thumbs import ThumbCandidate, limit_candidates, select_candidates
from nagare_clip.publish.timeline import build_edit_map

SEG_TIMES = [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0)]


def _placed(keeps, stem="a"):
    return build_edit_map([(stem, {"keep_intervals": [{"start": s, "end": e} for s, e in keeps]})])


def test_overlay_keep_and_timelapse_moments():
    placed = _placed([(0.0, 40.0)])
    ops = [
        DirectorOp(type="overlay", lines=(2, 2), text="水浸し！"),
        DirectorOp(type="keep", lines=(3, 4), note="cleanup"),
        DirectorOp(type="timelapse", lines=(1, 4), factor=8.0),
        DirectorOp(type="cut", lines=(1, 1)),  # not a payoff; contributes nothing
    ]
    got = [(c.source_time, c.reason) for c in select_candidates(ops, SEG_TIMES, placed, "a")]
    assert got == [
        (0.0, "timelapse start"),
        (10.0, "overlay: 水浸し！"),
        (30.0, "keep: cleanup"),  # midpoint of lines 3-4
        (39.8, "timelapse end"),  # inset: the interval end itself is exclusive
    ]


def test_moment_on_cut_footage_snaps_forward_within_its_op():
    # line 2 opens on cut footage; the candidate moves to the first kept frame
    placed = _placed([(0.0, 10.0), (15.0, 40.0)])
    ops = [DirectorOp(type="overlay", lines=(2, 2), text="x")]
    got = select_candidates(ops, SEG_TIMES, placed, "a")
    assert [(c.source_time, c.timeline_time) for c in got] == [(15.0, 10.0)]


def test_op_with_no_surviving_footage_drops_out():
    placed = _placed([(0.0, 10.0)])
    ops = [DirectorOp(type="overlay", lines=(3, 3), text="x")]
    assert select_candidates(ops, SEG_TIMES, placed, "a") == []


def test_out_of_range_lines_are_ignored():
    placed = _placed([(0.0, 40.0)])
    ops = [DirectorOp(type="overlay", lines=(9, 9), text="x")]
    assert select_candidates(ops, SEG_TIMES, placed, "a") == []


def test_near_duplicate_moments_collapse_onto_the_more_informative_one():
    placed = _placed([(0.0, 40.0)])
    ops = [
        DirectorOp(type="timelapse", lines=(2, 2), factor=4.0),  # start == 10.0 too
        DirectorOp(type="overlay", lines=(2, 2), text="a"),
    ]
    got = select_candidates(ops, SEG_TIMES, placed, "a")
    # the overlay wins the collision at 10.0 even though it was found second
    assert [(c.source_time, c.reason) for c in got] == [
        (10.0, "overlay: a"),
        (19.8, "timelapse end"),
    ]


def test_limit_spreads_across_the_video_rather_than_truncating():
    cands = [ThumbCandidate("a", float(i), float(i), "x") for i in range(10)]
    assert [c.source_time for c in limit_candidates(cands, 3)] == [0.0, 3.0, 6.0]
    assert limit_candidates(cands, 0) == cands
    assert limit_candidates(cands, 99) == cands


def test_relpath_is_stem_scoped():
    assert ThumbCandidate("talk1", 12.5, 3.0, "x").relpath == "frames/talk1/12.500.jpg"
