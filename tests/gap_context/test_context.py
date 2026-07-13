from nagare_clip.gap_context.context import (
    anchor_gaps,
    annotate_numbered_transcript,
    format_gap_block,
)
from nagare_clip.gap_context.gaps import Gap

SEG_TIMES = [(0.0, 10.0), (20.0, 25.0), (25.5, 30.0)]
GAP = Gap(start=10.0, end=20.0, frames=[], description="ビルドが走る")


def test_anchor_gaps_attaches_to_the_last_line_ending_before_the_gap():
    assert anchor_gaps([GAP], SEG_TIMES) == [(1, GAP)]


def test_anchor_gaps_before_the_first_line_anchors_to_zero():
    g = Gap(start=0.0, end=5.0, frames=[], description="d")
    assert anchor_gaps([g], [(6.0, 10.0)]) == [(0, g)]


def test_anchor_gaps_with_no_segment_times():
    assert anchor_gaps([GAP], []) == [(0, GAP)]


def test_anchor_gaps_empty():
    assert anchor_gaps([], SEG_TIMES) == []


def test_format_gap_block():
    assert format_gap_block([(1, GAP)]) == (
        "## Silent gaps (visual context)\n- after line 1 (10.0s-20.0s, 10.0s): ビルドが走る"
    )


def test_format_gap_block_before_the_first_line():
    g = Gap(start=0.0, end=5.0, frames=[], description="タイトル画面")
    assert format_gap_block([(0, g)]) == (
        "## Silent gaps (visual context)\n- before line 1 (0.0s-5.0s, 5.0s): タイトル画面"
    )


def test_format_gap_block_empty_is_empty_string():
    assert format_gap_block([]) == ""


def test_annotate_numbered_transcript_inserts_after_the_anchor_line():
    transcript = "1: いち  [10.0s, gap 10.0s]\n2: に  [5.0s, gap 0.5s]\n3: さん  [4.5s]"
    out = annotate_numbered_transcript(transcript, [(1, GAP)])
    assert out == (
        "1: いち  [10.0s, gap 10.0s]\n"
        "    [silent gap 10.0s: ビルドが走る]\n"
        "2: に  [5.0s, gap 0.5s]\n"
        "3: さん  [4.5s]"
    )


def test_annotate_numbered_transcript_anchor_zero_goes_first():
    g = Gap(start=0.0, end=5.0, frames=[], description="タイトル画面")
    out = annotate_numbered_transcript("1: いち", [(0, g)])
    assert out == "    [silent gap 5.0s: タイトル画面]\n1: いち"


def test_annotate_numbered_transcript_is_byte_identical_when_empty():
    transcript = "1: いち\n2: に"
    assert annotate_numbered_transcript(transcript, []) == transcript


def test_annotate_numbered_transcript_ignores_out_of_range_anchors():
    transcript = "1: いち"
    assert annotate_numbered_transcript(transcript, [(9, GAP)]) == transcript
