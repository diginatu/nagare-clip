import json

from nagare_clip.whisper.hints import line_hints, load_line_hints


def seg(text, t0, step=1.0):
    return {
        "text": text,
        "start": t0,
        "end": t0 + step * len(text),
        "words": [
            {"word": c, "start": t0 + i * step, "end": t0 + (i + 1) * step, "score": 1.0}
            for i, c in enumerate(text)
        ],
    }


def test_identical_lines_have_no_hint():
    segs = [seg("あいう", 0), seg("えお", 3)]
    assert line_hints(segs, [(0, 5)], ["あいうえお"]) == [None, None]


def test_substitution():
    segs = [seg("こんにちは", 0), seg("散乱感出してる", 5), seg("以上です", 12)]
    hints = line_hints(segs, [(0, 16)], ["こんにちは産卵管出してる以上です"])
    assert hints == [None, "産卵管出してる", None]


def test_line_spanning_two_chunks():
    segs = [seg("あいうえ", 0)]  # chars start 0,1,2,3
    hints = line_hints(segs, [(0, 2), (2, 4)], ["あX", "うえ"])
    assert hints == ["あXうえ"]


def test_insertion_goes_to_earlier_line():
    segs = [seg("あいう", 0), seg("えお", 3)]
    hints = line_hints(segs, [(0, 5)], ["あいうXえお"])
    assert hints == ["あいうX", None]


def test_whisper_empty_chunk_gives_empty_hint():
    segs = [seg("あいう", 0), seg("えお", 3)]
    assert line_hints(segs, [(0, 3), (3, 5)], ["あいう", ""]) == [None, ""]


def test_char_outside_all_chunks_uses_nearest():
    segs = [seg("あい", 0), seg("うえ", 10)]
    # chars at 10, 11 are outside both chunks; nearest chunk is the second
    hints = line_hints(segs, [(0, 2), (4, 6)], ["あい", "うX"])
    assert hints == [None, "うX"]


def test_load_missing_file_returns_none(tmp_path):
    assert load_line_hints(tmp_path / "a.json", tmp_path / "b.json") is None


def test_load_roundtrip_and_bad_counts(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"segments": [seg("あい", 0)]}))
    b.write_text(json.dumps({"chunks": [[0, 2]], "texts": ["あX"]}))
    assert load_line_hints(a, b) == ["あX"]
    b.write_text(json.dumps({"chunks": [[0, 2]], "texts": []}))
    assert load_line_hints(a, b) is None


def test_word_without_start_uses_previous_start():
    s = seg("あいうえ", 0)
    del s["words"][1]["start"]
    del s["words"][0]["start"]  # falls back to segment start
    assert line_hints([s], [(0, 2), (2, 4)], ["あい", "うX"]) == ["あいうX"]


def test_multi_char_word_entry_is_expanded():
    s = {
        "text": "あいう",
        "start": 0,
        "end": 3,
        "words": [{"word": "あい", "start": 0}, {"word": " う", "start": 2}],
    }
    assert line_hints([s, seg("えお", 3)], [(0, 5)], ["あいうXえお"]) == ["あいうX", None]


def test_unequal_replace_block_covers_whole_span():
    segs = [seg("あい", 0), seg("うえ", 2)]
    hints = line_hints(segs, [(0, 4)], ["あXYZえ"])
    assert "".join(h or "" for h in hints) != ""
    assert hints[0] is not None and hints[1] is not None


def test_load_malformed_segments_returns_none(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"segments": [{"text": "あ", "words": [5]}]}))
    b.write_text(json.dumps({"chunks": [[0, 2]], "texts": ["あ"]}))
    assert load_line_hints(a, b) is None


def test_punctuation_only_difference_has_no_hint():
    segs = [seg("あ、入ったかこれは", 0), seg("じゃあね。バイバイ。", 10), seg("本当?", 25)]
    texts = ["あ入ったかこれは", "じゃあねバイバイ", "本当？"]
    hints = line_hints(segs, [(0, 9), (10, 20), (25, 28)], texts)
    assert hints == [None, None, None]


def test_whitespace_only_difference_has_no_hint():
    segs = [seg("あいう", 0)]
    assert line_hints(segs, [(0, 3)], ["あ い　う"]) == [None]


def test_prolonged_sound_mark_is_not_punctuation():
    segs = [seg("下がったね", 0)]
    assert line_hints(segs, [(0, 5)], ["下がったねー"]) == ["下がったねー"]


def test_real_difference_hint_has_punctuation_removed():
    segs = [seg("あ、産卵感。", 0)]
    assert line_hints(segs, [(0, 6)], ["あ、産卵管。"]) == ["あ産卵管"]


def test_hint_keeps_prolonged_sound_mark_when_other_text_differs():
    segs = [seg("散乱感ー", 0)]
    assert line_hints(segs, [(0, 4)], ["産卵管ー"]) == ["産卵管ー"]


def test_hint_has_whitespace_removed():
    segs = [seg("散乱感", 0)]
    assert line_hints(segs, [(0, 3)], ["産卵 管"]) == ["産卵管"]


def test_empty_whisper_text_for_speech_line_stays_empty():
    segs = [seg("あいう", 0)]
    assert line_hints(segs, [(0, 3)], [""]) == [""]
