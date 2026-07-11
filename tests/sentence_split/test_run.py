import json

from nagare_clip.sentence_split import run as ss_run
from nagare_clip.sentence_split.segment import concat_word_text


def _seg(text, t0):
    return {
        "start": float(t0),
        "end": float(t0 + len(text)),
        "text": text,
        "words": [
            {"word": ch, "start": float(t0 + i), "end": float(t0 + i + 1), "score": 1.0}
            for i, ch in enumerate(text)
        ],
    }


def _data():
    # two WhisperX segments that together form two sentences, with a real 3s
    # word gap between them (お ends 5.0, か starts 8.0)
    return {"language": "ja", "segments": [_seg("あいうえお", 0), _seg("かきくけこ", 8)]}


def test_resegment_rebuilds_and_preserves_text(monkeypatch):
    data = _data()

    # Stub bunsetsu + LLM so the test is deterministic (no GiNZA/model needed).
    monkeypatch.setattr(
        ss_run, "bunsetsu_units", lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)]
    )
    monkeypatch.setattr(
        ss_run, "split_window", lambda bunsetsu, cfg, **kw: [(0, 2), (3, len(bunsetsu) - 1)]
    )

    sp_cfg = {"enabled": True, "window_segments": 20}
    out = ss_run.resegment_json(data, sp_cfg, nlp=None, recorder=ss_run.NULL_RECORDER, stem="x")
    # 10 chars, split after index 2 -> 2 new segments, text fully preserved
    assert [s["text"] for s in out["segments"]] == ["あいう", "えおかきくけこ"]
    assert concat_word_text(out["segments"]) == "あいうえおかきくけこ"
    assert out["word_segments"] == [w for s in out["segments"] for w in s["words"]]


def test_resegment_force_splits_llm_output_at_silence(monkeypatch):
    # LLM groups everything into one sentence, but a silence in the word gap
    # (midpoint 6.5 -> first word start>=6.5 is "か" at 8.0, prev end 5.0)
    # forces a split.
    data = _data()
    monkeypatch.setattr(
        ss_run, "bunsetsu_units", lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)]
    )
    monkeypatch.setattr(
        ss_run, "split_window", lambda bunsetsu, cfg, **kw: [(0, len(bunsetsu) - 1)]
    )
    out = ss_run.resegment_json(
        data,
        {"enabled": True, "window_segments": 20},
        nlp=None,
        recorder=ss_run.NULL_RECORDER,
        stem="x",
        silences=[(5.0, 8.0)],
    )
    assert [s["text"] for s in out["segments"]] == ["あいうえお", "かきくけこ"]
    assert concat_word_text(out["segments"]) == "あいうえおかきくけこ"


def test_resegment_force_splits_degraded_fallback(monkeypatch):
    # Even when every window degrades (None), the forced split still applies to
    # the fallback original segments. Seg1 has an internal 3s word gap (う ends
    # 3.0, え starts 6.0) that silence midpoint 4.5 corroborates.
    seg1 = {
        "start": 0.0,
        "end": 8.0,
        "text": "あいうえお",
        "words": [
            {"word": "あ", "start": 0.0, "end": 1.0, "score": 1.0},
            {"word": "い", "start": 1.0, "end": 2.0, "score": 1.0},
            {"word": "う", "start": 2.0, "end": 3.0, "score": 1.0},
            {"word": "え", "start": 6.0, "end": 7.0, "score": 1.0},
            {"word": "お", "start": 7.0, "end": 8.0, "score": 1.0},
        ],
    }
    data = {"language": "ja", "segments": [seg1, _seg("かきくけこ", 10)]}
    monkeypatch.setattr(
        ss_run, "bunsetsu_units", lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)]
    )
    monkeypatch.setattr(ss_run, "split_window", lambda bunsetsu, cfg, **kw: None)
    out = ss_run.resegment_json(
        data,
        {"enabled": True, "window_segments": 20},
        nlp=None,
        recorder=ss_run.NULL_RECORDER,
        stem="x",
        silences=[(3.05, 5.95)],
    )
    assert [s["text"] for s in out["segments"]] == ["あいう", "えお", "かきくけこ"]
    assert concat_word_text(out["segments"]) == "あいうえおかきくけこ"


def test_resegment_no_silences_is_unchanged(monkeypatch):
    # Regression guard: silences=[] (and the default) must match today exactly.
    data = _data()
    monkeypatch.setattr(
        ss_run, "bunsetsu_units", lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)]
    )
    monkeypatch.setattr(
        ss_run, "split_window", lambda bunsetsu, cfg, **kw: [(0, 2), (3, len(bunsetsu) - 1)]
    )
    cfg = {"enabled": True, "window_segments": 20}
    baseline = ss_run.resegment_json(
        _data(), cfg, nlp=None, recorder=ss_run.NULL_RECORDER, stem="x"
    )
    out = ss_run.resegment_json(
        data, cfg, nlp=None, recorder=ss_run.NULL_RECORDER, stem="x", silences=[]
    )
    assert [s["text"] for s in out["segments"]] == [s["text"] for s in baseline["segments"]]
    assert [s["text"] for s in out["segments"]] == ["あいう", "えおかきくけこ"]


def test_resegment_degraded_window_keeps_original(monkeypatch):
    data = _data()
    monkeypatch.setattr(
        ss_run, "bunsetsu_units", lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)]
    )
    monkeypatch.setattr(ss_run, "split_window", lambda bunsetsu, cfg, **kw: None)
    out = ss_run.resegment_json(
        data,
        {"enabled": True, "window_segments": 20},
        nlp=None,
        recorder=ss_run.NULL_RECORDER,
        stem="x",
    )
    assert [s["text"] for s in out["segments"]] == ["あいうえお", "かきくけこ"]


def _stub_bunsetsu(monkeypatch):
    monkeypatch.setattr(
        ss_run, "bunsetsu_units", lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)]
    )


def _surface(bunsetsu):
    return "".join(s for _, _, s in bunsetsu)


def test_carry_over_merges_sentence_across_window_boundary(monkeypatch):
    # window_segments=2 -> win1=[あい, うえ], win2=[お].  The sentence うえお
    # straddles the seam; carry-over must rejoin it.
    data = {"language": "ja", "segments": [_seg("あい", 0), _seg("うえ", 2), _seg("お", 4)]}
    _stub_bunsetsu(monkeypatch)

    def split(bunsetsu, cfg, **kw):
        s = _surface(bunsetsu)
        if s == "あいうえ":
            return [(0, 1), (2, 3)]  # あい | うえ  -> carry うえ
        if s == "うえお":
            return [(0, 2)]  # うえお as one sentence
        raise AssertionError(f"unexpected surface {s!r}")

    monkeypatch.setattr(ss_run, "split_window", split)
    out = ss_run.resegment_json(
        data,
        {"enabled": True, "window_segments": 2},
        nlp=None,
        recorder=ss_run.NULL_RECORDER,
        stem="x",
    )
    assert [s["text"] for s in out["segments"]] == ["あい", "うえお"]
    assert concat_word_text(out["segments"]) == "あいうえお"


def test_single_sentence_window_is_not_carried(monkeypatch):
    # window_segments=1; each window comes back as one sentence -> no carry,
    # the boundary is accepted as a real break (run-on guard).
    data = {"language": "ja", "segments": [_seg("あい", 0), _seg("うえ", 2)]}
    _stub_bunsetsu(monkeypatch)

    def split(bunsetsu, cfg, **kw):
        s = _surface(bunsetsu)
        if s in ("あい", "うえ"):
            return [(0, 1)]
        if s == "あいうえ":  # only reached if the guard is wrongly removed
            return [(0, 3)]
        raise AssertionError(f"unexpected surface {s!r}")

    monkeypatch.setattr(ss_run, "split_window", split)
    out = ss_run.resegment_json(
        data,
        {"enabled": True, "window_segments": 1},
        nlp=None,
        recorder=ss_run.NULL_RECORDER,
        stem="x",
    )
    assert [s["text"] for s in out["segments"]] == ["あい", "うえ"]
    assert concat_word_text(out["segments"]) == "あいうえ"


def test_degraded_window_flushes_carried_sentence(monkeypatch):
    # win1 re-splits across the seg boundary (あ | いうえ) and carries いうえ;
    # win2 degrades (None) and must flush the carried sentence, not drop it.
    data = {"language": "ja", "segments": [_seg("あい", 0), _seg("うえ", 2), _seg("お", 4)]}
    _stub_bunsetsu(monkeypatch)

    def split(bunsetsu, cfg, **kw):
        if _surface(bunsetsu) == "あいうえ":
            return [(0, 0), (1, 3)]  # あ | いうえ -> carry いうえ
        return None  # win2 degrades

    monkeypatch.setattr(ss_run, "split_window", split)
    out = ss_run.resegment_json(
        data,
        {"enabled": True, "window_segments": 2},
        nlp=None,
        recorder=ss_run.NULL_RECORDER,
        stem="x",
    )
    assert [s["text"] for s in out["segments"]] == ["あ", "いうえ", "お"]
    assert concat_word_text(out["segments"]) == "あいうえお"


def test_enabled_resegmentation_writes_new_json_and_txt(tmp_path, monkeypatch):
    data = _data()
    in_json = tmp_path / "in.json"
    in_txt = tmp_path / "in.txt"
    in_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    in_txt.write_text("あいうえお\nかきくけこ\n", encoding="utf-8")
    out_json = tmp_path / "out.json"
    out_txt = tmp_path / "out.txt"

    monkeypatch.setattr(ss_run, "load_nlp", lambda: None)
    monkeypatch.setattr(
        ss_run, "bunsetsu_units", lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)]
    )
    monkeypatch.setattr(
        ss_run, "split_window", lambda bunsetsu, cfg, **kw: [(0, 2), (3, len(bunsetsu) - 1)]
    )

    ss_run.run_sentence_split(
        in_json,
        in_txt,
        out_json,
        out_txt,
        {"sentence_split": {"enabled": True, "window_segments": 20}},
        stem="x",
    )

    new_data = json.loads(out_json.read_text(encoding="utf-8"))
    assert [s["text"] for s in new_data["segments"]] == ["あいう", "えおかきくけこ"]
    assert out_txt.read_text(encoding="utf-8") == "あいう\nえおかきくけこ\n"


def _write_inputs(tmp_path):
    in_json = tmp_path / "in.json"
    in_txt = tmp_path / "in.txt"
    in_json.write_text(json.dumps(_data(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    in_txt.write_text("あいうえお\nかきくけこ\n", encoding="utf-8")
    return in_json, in_txt


def _stub_llm_single_sentence(monkeypatch):
    # LLM groups every window into a single sentence, so any split in the output
    # can only come from the forced silence split.
    monkeypatch.setattr(ss_run, "load_nlp", lambda: None)
    monkeypatch.setattr(
        ss_run, "bunsetsu_units", lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)]
    )
    monkeypatch.setattr(
        ss_run, "split_window", lambda bunsetsu, cfg, **kw: [(0, len(bunsetsu) - 1)]
    )


def test_run_force_splits_at_qualifying_cut(tmp_path, monkeypatch):
    in_json, in_txt = _write_inputs(tmp_path)
    out_json, out_txt = tmp_path / "out.json", tmp_path / "out.txt"
    _stub_llm_single_sentence(monkeypatch)

    from nagare_clip.audio_silence.cuts_file import write_cuts

    cuts = tmp_path / "in_cuts.txt"
    write_cuts(cuts, [(5.0, 8.0)])  # 3.0s span in the word gap, midpoint 6.5

    ss_run.run_sentence_split(
        in_json,
        in_txt,
        out_json,
        out_txt,
        {
            "sentence_split": {
                "enabled": True,
                "window_segments": 20,
                "force_split": True,
                "force_split_min_silence": 0.5,
            }
        },
        stem="x",
        cuts_txt=cuts,
    )
    assert out_txt.read_text(encoding="utf-8") == "あいうえお\nかきくけこ\n"


def test_run_ignores_sub_threshold_cut(tmp_path, monkeypatch):
    in_json, in_txt = _write_inputs(tmp_path)
    out_json, out_txt = tmp_path / "out.json", tmp_path / "out.txt"
    _stub_llm_single_sentence(monkeypatch)

    from nagare_clip.audio_silence.cuts_file import write_cuts

    cuts = tmp_path / "in_cuts.txt"
    # 2.0s span in the word gap (would split if it qualified), below the
    # 3.0s threshold
    write_cuts(cuts, [(5.5, 7.5)])

    ss_run.run_sentence_split(
        in_json,
        in_txt,
        out_json,
        out_txt,
        {
            "sentence_split": {
                "enabled": True,
                "window_segments": 20,
                "force_split": True,
                "force_split_min_silence": 3.0,
            }
        },
        stem="x",
        cuts_txt=cuts,
    )
    # No qualifying silence -> LLM's single sentence stands.
    assert out_txt.read_text(encoding="utf-8") == "あいうえおかきくけこ\n"


def test_run_force_split_disabled_ignores_cuts(tmp_path, monkeypatch):
    in_json, in_txt = _write_inputs(tmp_path)
    out_json, out_txt = tmp_path / "out.json", tmp_path / "out.txt"
    _stub_llm_single_sentence(monkeypatch)

    from nagare_clip.audio_silence.cuts_file import write_cuts

    cuts = tmp_path / "in_cuts.txt"
    write_cuts(cuts, [(5.0, 8.0)])  # would split if force_split were honored

    ss_run.run_sentence_split(
        in_json,
        in_txt,
        out_json,
        out_txt,
        {
            "sentence_split": {
                "enabled": True,
                "window_segments": 20,
                "force_split": False,
                "force_split_min_silence": 0.5,
            }
        },
        stem="x",
        cuts_txt=cuts,
    )
    assert out_txt.read_text(encoding="utf-8") == "あいうえおかきくけこ\n"


def test_run_missing_cuts_file_is_noop(tmp_path, monkeypatch):
    in_json, in_txt = _write_inputs(tmp_path)
    out_json, out_txt = tmp_path / "out.json", tmp_path / "out.txt"
    _stub_llm_single_sentence(monkeypatch)

    ss_run.run_sentence_split(
        in_json,
        in_txt,
        out_json,
        out_txt,
        {
            "sentence_split": {
                "enabled": True,
                "window_segments": 20,
                "force_split": True,
                "force_split_min_silence": 0.5,
            }
        },
        stem="x",
        cuts_txt=tmp_path / "does_not_exist_cuts.txt",
    )
    assert out_txt.read_text(encoding="utf-8") == "あいうえおかきくけこ\n"


def test_disabled_copy_through_byte_identical(tmp_path):
    in_json = tmp_path / "in.json"
    in_txt = tmp_path / "in.txt"
    in_json.write_text(json.dumps(_data(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    in_txt.write_text("あいうえお\nかきくけこ\n", encoding="utf-8")
    out_json = tmp_path / "out.json"
    out_txt = tmp_path / "out.txt"

    ss_run.run_sentence_split(
        in_json,
        in_txt,
        out_json,
        out_txt,
        {"sentence_split": {"enabled": False}},
        stem="x",
    )
    assert out_json.read_bytes() == in_json.read_bytes()
    assert out_txt.read_bytes() == in_txt.read_bytes()
