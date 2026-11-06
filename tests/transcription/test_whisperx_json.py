from nagare_clip.transcription.whisperx_json import build_whisperx_json, transcript_text

REGIONS = [(1.0, 2.0), (5.0, 6.0)]


def tok(text, start, end):
    return {"text": text, "start": start, "end": end}


def test_one_segment_per_region_and_char_words():
    data = build_whisperx_json(
        [tok("は", 1.0, 1.2), tok("いい", 1.2, 1.4), tok("今", 5.1, 5.3)], REGIONS
    )
    assert data["language"] == "ja"
    assert len(data["segments"]) == 2
    seg0 = data["segments"][0]
    assert seg0["text"] == "はいい"
    assert [(w["word"], w["start"], w["end"]) for w in seg0["words"]] == [
        ("は", 1.0, 1.2),
        ("い", 1.2, 1.3),
        ("い", 1.3, 1.4),
    ]
    assert all(w["score"] == 1.0 for w in seg0["words"])
    assert seg0["start"] == 1.0 and seg0["end"] == 1.4
    assert data["word_segments"] == [w for s in data["segments"] for w in s["words"]]


def test_gap_token_goes_to_nearer_region():
    data = build_whisperx_json([tok("あ", 4.8, 4.9), tok("い", 2.1, 2.2)], REGIONS)
    assert [s["text"] for s in data["segments"]] == ["い", "あ"]
    assert "".join(s["text"] for s in data["segments"]) == "いあ"


def test_gap_tie_goes_to_earlier_region():
    data = build_whisperx_json([tok("あ", 3.5, 3.6)], REGIONS)
    assert len(data["segments"]) == 1 and data["segments"][0]["start"] == 3.5
    data = build_whisperx_json([tok("あ", 3.5, 3.6), tok("い", 5.0, 5.1)], REGIONS)
    assert [s["text"] for s in data["segments"]] == ["あ", "い"]


def test_zero_duration_token():
    data = build_whisperx_json([tok("。", 3.0, 3.0)], REGIONS)
    (w,) = data["segments"][0]["words"]
    assert w["start"] == w["end"] == 3.0


def test_regions_without_tokens_make_no_segment():
    data = build_whisperx_json([tok("今", 5.1, 5.3)], REGIONS)
    assert len(data["segments"]) == 1
    assert data["segments"][0]["text"] == "今"


def test_no_tokens_gives_empty_segments():
    data = build_whisperx_json([], REGIONS)
    assert data["segments"] == []
    assert transcript_text(data) == ""


def test_transcript_text():
    data = build_whisperx_json([tok("あ", 1.0, 1.1), tok("い", 5.0, 5.1)], REGIONS)
    assert transcript_text(data) == "あ\nい\n"


def test_whitespace_is_dropped_and_time_split_over_remaining_chars():
    data = build_whisperx_json([tok(" 今 a", 1.0, 1.4)], REGIONS)
    seg = data["segments"][0]
    assert seg["text"] == "今a"
    assert [(w["word"], w["start"], w["end"]) for w in seg["words"]] == [
        ("今", 1.0, 1.2),
        ("a", 1.2, 1.4),
    ]


def test_whitespace_only_token_contributes_nothing():
    data = build_whisperx_json([tok("　", 1.0, 1.1), tok(" ", 5.0, 5.1)], REGIONS)
    assert data["segments"] == []
    data = build_whisperx_json([tok("　", 1.0, 1.1), tok("今", 5.1, 5.3)], REGIONS)
    assert [s["text"] for s in data["segments"]] == ["今"]


def test_many_regions_assignment():
    regions = [(10.0 * i, 10.0 * i + 4.0) for i in range(1000)]
    toks = [
        tok("あ", 5000.5, 5000.6),  # inside region 500
        tok("い", 7.5, 7.6),  # gap 4..10: nearer region 1 (10)
        tok("う", 1.5, 1.6),  # gap 4..10 of region 0: nearer region 0 (end 4? 2.5 vs 8.5)
        tok("え", 7.0, 7.1),  # exact tie between region 0 end 4 and ... (3 vs 3) -> earlier
        tok("お", 9999.0, 9999.1),  # past the last region
    ]
    data = build_whisperx_json(toks, regions)
    got = {s["text"]: s["start"] for s in data["segments"]}
    by_seg = {s["words"][0]["word"]: i for i, s in enumerate(data["segments"])}
    assert [s["text"] for s in data["segments"]] == ["うえ", "い", "あ", "お"]
    assert got["うえ"] == 1.5 and got["い"] == 7.5
    assert by_seg["あ"] == 2 and by_seg["お"] == 3
