from nagare_clip.sentence_split.segment import (
    char_to_word_index,
    concat_word_text,
    iter_windows,
    rebuild_window_segments,
    segment_from_words,
    split_segment_at_silences,
    window_text_and_words,
)


def _w(ch, start, end):
    return {"word": ch, "start": start, "end": end, "score": 1.0}


def test_char_to_word_index_handles_multichar_and_space():
    words = [{"word": "ab"}, {"word": " "}, {"word": "c"}]
    assert char_to_word_index(words) == [0, 0, 1, 2]


def test_window_text_and_words_concatenates():
    win = [{"words": [_w("あ", 0, 1), _w("い", 1, 2)]}, {"words": [_w("う", 2, 3)]}]
    text, words = window_text_and_words(win)
    assert text == "あいう"
    assert len(words) == 3


def test_iter_windows_chunks_whole_segments():
    segs = list(range(5))
    assert list(iter_windows(segs, 2)) == [(0, [0, 1]), (2, [2, 3]), (4, [4])]


def test_rebuild_splits_at_bunsetsu_boundaries():
    # text "あいうえお", 5 single-char words; 2 bunsetsu split after char 2.
    words = [
        _w("あ", 0.0, 0.5),
        _w("い", 0.5, 1.0),
        _w("う", 1.0, 1.5),
        _w("え", 1.5, 2.0),
        _w("お", 2.0, 2.5),
    ]
    bunsetsu = [(0, 2, "あい"), (2, 5, "うえお")]
    char2word = char_to_word_index(words)
    ranges = [(0, 0), (1, 1)]
    segs = rebuild_window_segments(words, bunsetsu, ranges, char2word)
    assert [s["text"] for s in segs] == ["あい", "うえお"]
    assert segs[0]["start"] == 0.0 and segs[0]["end"] == 1.0
    assert segs[1]["start"] == 1.0 and segs[1]["end"] == 2.5
    # words preserved, nothing duplicated or lost
    assert concat_word_text(segs) == "あいうえお"


def test_rebuild_single_range_is_whole_window():
    words = [_w("あ", 0, 1), _w("い", 1, 2)]
    bunsetsu = [(0, 2, "あい")]
    segs = rebuild_window_segments(words, bunsetsu, [(0, 0)], char_to_word_index(words))
    assert [s["text"] for s in segs] == ["あい"]


def _seg5():
    # "あいうえお": five 1-second words at 0,1,2,3,4 (no internal gap).
    return segment_from_words(
        [
            _w("あ", 0.0, 1.0),
            _w("い", 1.0, 2.0),
            _w("う", 2.0, 3.0),
            _w("え", 3.0, 4.0),
            _w("お", 4.0, 5.0),
        ]
    )


def _seg_gap():
    # "あいうえお" with a real 3s inter-word gap between う (ends 3.0) and
    # え (starts 6.0) — the shape a genuine pause has in WhisperX timings.
    return segment_from_words(
        [
            _w("あ", 0.0, 1.0),
            _w("い", 1.0, 2.0),
            _w("う", 2.0, 3.0),
            _w("え", 6.0, 7.0),
            _w("お", 7.0, 8.0),
        ]
    )


def test_split_at_silence_splits_at_corroborated_gap():
    # silence (3.05, 5.95): midpoint 4.5 falls in the う/え word gap -> split
    # before え.
    segs = split_segment_at_silences(_seg_gap(), [(3.05, 5.95)])
    assert [s["text"] for s in segs] == ["あいう", "えお"]
    assert segs[0]["start"] == 0.0 and segs[0]["end"] == 3.0
    assert segs[1]["start"] == 6.0 and segs[1]["end"] == 8.0
    assert concat_word_text(segs) == "あいうえお"


def test_split_uses_midpoint_not_span_edges():
    # silence (0.5, 8.5): only its midpoint 4.5 lies in the う/え gap. Using
    # the span start (0.5) or end (8.5) instead would find no corroborated
    # gap (or no word at all), so this pins the midpoint on both the word
    # comparison and the gap check.
    segs = split_segment_at_silences(_seg_gap(), [(0.5, 8.5)])
    assert [s["text"] for s in segs] == ["あいう", "えお"]


def test_split_skipped_when_word_spans_silence():
    # WhisperX often stretches one word across an entire pause (e.g. は timed
    # 2.06-7.40 over a 2.23-6.50 silence). The midpoint then falls *inside*
    # that word, not in a gap — splitting before the next word would chop the
    # sentence one word too late. Ambiguous -> no split.
    seg = segment_from_words(
        [
            _w("ど", 2.0, 3.0),
            _w("こ", 3.0, 9.0),  # stretched across the silence
            _w("ん", 9.0, 9.02),
            _w("な", 9.02, 9.04),
        ]
    )
    segs = split_segment_at_silences(seg, [(3.2, 8.8)])
    assert [s["text"] for s in segs] == ["どこんな"]


def test_split_at_silence_before_segment_is_noop():
    # A silence entirely before the segment would put every word past its
    # midpoint (boundary at word 0); that boundary must be dropped, not emit an
    # empty leading segment.
    seg = segment_from_words([_w("か", 5.0, 6.0), _w("き", 6.0, 7.0), _w("く", 7.0, 8.0)])
    segs = split_segment_at_silences(seg, [(2.0, 3.0)])
    assert [s["text"] for s in segs] == ["かきく"]


def test_split_at_silence_outside_range_is_noop():
    segs = split_segment_at_silences(_seg5(), [(10.0, 11.0)])
    assert [s["text"] for s in segs] == ["あいうえお"]


def test_split_at_silence_empty_list_is_noop():
    segs = split_segment_at_silences(_seg5(), [])
    assert [s["text"] for s in segs] == ["あいうえお"]


def test_split_at_multiple_silences_in_one_segment():
    # Two gaps (1-4 and 5-8), one silence midpoint in each.
    seg = segment_from_words([_w("あ", 0.0, 1.0), _w("い", 4.0, 5.0), _w("う", 8.0, 9.0)])
    segs = split_segment_at_silences(seg, [(1.1, 3.9), (5.1, 7.9)])
    assert [s["text"] for s in segs] == ["あ", "い", "う"]
    assert concat_word_text(segs) == "あいう"


def test_split_at_silence_preserves_word_identity():
    seg = _seg_gap()
    originals = list(seg["words"])
    segs = split_segment_at_silences(seg, [(3.05, 5.95)])
    assert len(segs) == 2
    passed_through = [w for s in segs for w in s["words"]]
    assert passed_through == originals
    assert all(a is b for a, b in zip(passed_through, originals))


def test_split_at_silence_untimed_word_inherits_last_time():
    # The middle word has no timings; it must inherit the previous word's time
    # so the split never lands between a timed word and a following untimed one.
    words = [_w("あ", 0.0, 1.0), {"word": "い", "score": 1.0}, _w("う", 2.0, 3.0)]
    seg = segment_from_words(words)
    # silence midpoint 1.5: only "う" (start 2.0) is past it; "い" inherits 0.0.
    segs = split_segment_at_silences(seg, [(1.2, 1.8)])
    assert [s["text"] for s in segs] == ["あい", "う"]
