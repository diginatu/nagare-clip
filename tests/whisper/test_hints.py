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
