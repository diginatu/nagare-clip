from nagare_clip.sentence_split.segment import (
    char_to_word_index,
    concat_word_text,
    iter_windows,
    rebuild_window_segments,
    window_text_and_words,
)


def _w(ch, start, end):
    return {"word": ch, "start": start, "end": end, "score": 1.0}


def test_char_to_word_index_handles_multichar_and_space():
    words = [{"word": "ab"}, {"word": " "}, {"word": "c"}]
    assert char_to_word_index(words) == [0, 0, 1, 2]


def test_window_text_and_words_concatenates():
    win = [{"words": [_w("あ", 0, 1), _w("い", 1, 2)]},
           {"words": [_w("う", 2, 3)]}]
    text, words = window_text_and_words(win)
    assert text == "あいう"
    assert len(words) == 3


def test_iter_windows_chunks_whole_segments():
    segs = list(range(5))
    assert list(iter_windows(segs, 2)) == [(0, [0, 1]), (2, [2, 3]), (4, [4])]


def test_rebuild_splits_at_bunsetsu_boundaries():
    # text "あいうえお", 5 single-char words; 2 bunsetsu split after char 2.
    words = [_w("あ", 0.0, 0.5), _w("い", 0.5, 1.0), _w("う", 1.0, 1.5),
             _w("え", 1.5, 2.0), _w("お", 2.0, 2.5)]
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
    segs = rebuild_window_segments(words, bunsetsu, [(0, 0)],
                                   char_to_word_index(words))
    assert [s["text"] for s in segs] == ["あい"]
