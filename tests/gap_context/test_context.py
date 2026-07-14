from nagare_clip.gap_context.context import (
    anchor_gaps,
    annotate_numbered_transcript,
    format_gap_block,
)
from nagare_clip.gap_context.gaps import Gap, gaps_from_dict

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


def test_anchor_gaps_picks_the_last_qualifying_line_not_first():
    """Regression: anchor_gaps should pick LAST matching line, not first."""
    # Multiple lines end before the gap start—should anchor to the LAST one
    seg_times = [(0.0, 3.0), (3.5, 6.0), (6.5, 10.0), (20.0, 25.0)]
    gap = Gap(start=10.0, end=20.0, frames=[], description="test")
    anchored = anchor_gaps([gap], seg_times)
    # Line 3 (ends at 10.0) is the last to match, not line 1 (ends at 3.0)
    assert anchored == [(3, gap)]


def test_anchor_gaps_epsilon_allows_slight_overshoot():
    """Regression: _EPS epsilon must be load-bearing in the anchor condition."""
    # Line ends at 10.005, gap starts at 10.0—within _EPS, should still anchor
    seg_times = [(0.0, 10.005), (20.0, 25.0)]
    gap = Gap(start=10.0, end=20.0, frames=[], description="test")
    anchored = anchor_gaps([gap], seg_times)
    assert anchored == [(1, gap)]


def test_annotate_numbered_transcript_gap_after_final_line():
    """Regression: gaps anchored to len(lines) (trailing silence) must be appended."""
    transcript = "1: いち\n2: に"
    gap = Gap(start=30.0, end=35.0, frames=[], description="outro")
    # Anchor = 2, which equals len(lines); should append after the final line
    out = annotate_numbered_transcript(transcript, [(2, gap)])
    assert out == ("1: いち\n2: に\n    [silent gap 5.0s: outro]")


def test_annotate_numbered_transcript_never_injects_a_fake_numbered_line():
    """End-to-end-ish: a multi-line description read off a hand-edited
    gaps.json (gaps_from_dict is where the file's whitespace gets collapsed)
    must not, once spliced into the director's numbered transcript, produce a
    physical line that starts with a digit and a colon -- that would look like
    a real 'N: ...' transcript line to the director and break its unambiguous
    line-number contract."""
    raw = {
        "gaps": [
            {
                "start": 10.0,
                "end": 20.0,
                "description": "Something happens.\n2: fake injected line\nmore text",
            }
        ]
    }
    gaps = gaps_from_dict(raw)
    transcript = "1: いち  [10.0s, gap 10.0s]\n2: に  [5.0s]"
    anchored = anchor_gaps(gaps, [(0.0, 10.0), (20.0, 25.0)])
    out = annotate_numbered_transcript(transcript, anchored)

    real_lines = set(transcript.split("\n"))
    for line in out.split("\n"):
        stripped = line.lstrip()
        if stripped[:1].isdigit() and ":" in stripped:
            assert line in real_lines, f"fake numbered line injected: {line!r}"


def test_annotate_numbered_transcript_multiple_gaps_same_line():
    """Regression: multiple gaps anchored to the same line must all appear."""
    transcript = "1: いち\n2: に"
    gap1 = Gap(start=10.0, end=15.0, frames=[], description="first")
    gap2 = Gap(start=15.0, end=20.0, frames=[], description="second")
    out = annotate_numbered_transcript(transcript, [(1, gap1), (1, gap2)])
    # Both gaps should appear after line 1, in order
    assert out == ("1: いち\n    [silent gap 5.0s: first]\n    [silent gap 5.0s: second]\n2: に")
