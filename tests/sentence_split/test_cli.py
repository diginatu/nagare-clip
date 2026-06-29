import json
from pathlib import Path

from nagare_clip.sentence_split import cli as ss_cli
from nagare_clip.sentence_split.segment import concat_word_text


def _seg(text, t0):
    return {
        "start": float(t0), "end": float(t0 + len(text)), "text": text,
        "words": [{"word": ch, "start": float(t0 + i), "end": float(t0 + i + 1),
                   "score": 1.0} for i, ch in enumerate(text)],
    }


def _data():
    # two WhisperX segments that together form two sentences
    return {"language": "ja", "segments": [_seg("あいうえお", 0), _seg("かきくけこ", 5)]}


def test_resegment_rebuilds_and_preserves_text(monkeypatch):
    data = _data()

    # Stub bunsetsu + LLM so the test is deterministic (no GiNZA/model needed).
    monkeypatch.setattr(ss_cli, "bunsetsu_units",
                        lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)])
    monkeypatch.setattr(ss_cli, "split_window",
                        lambda bunsetsu, cfg, **kw: [(0, 2), (3, len(bunsetsu) - 1)])

    sp_cfg = {"enabled": True, "window_segments": 20}
    out = ss_cli.resegment_json(data, sp_cfg, nlp=None, recorder=ss_cli.NULL_RECORDER, stem="x")
    # 10 chars, split after index 2 -> 2 new segments, text fully preserved
    assert [s["text"] for s in out["segments"]] == ["あいう", "えおかきくけこ"]
    assert concat_word_text(out["segments"]) == "あいうえおかきくけこ"
    assert out["word_segments"] == [w for s in out["segments"] for w in s["words"]]


def test_resegment_degraded_window_keeps_original(monkeypatch):
    data = _data()
    monkeypatch.setattr(ss_cli, "bunsetsu_units",
                        lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)])
    monkeypatch.setattr(ss_cli, "split_window", lambda bunsetsu, cfg, **kw: None)
    out = ss_cli.resegment_json(data, {"enabled": True, "window_segments": 20},
                                nlp=None, recorder=ss_cli.NULL_RECORDER, stem="x")
    assert [s["text"] for s in out["segments"]] == ["あいうえお", "かきくけこ"]


def test_disabled_copy_through_byte_identical(tmp_path, monkeypatch):
    in_json = tmp_path / "in.json"
    in_txt = tmp_path / "in.txt"
    in_json.write_text(json.dumps(_data(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    in_txt.write_text("あいうえお\nかきくけこ\n", encoding="utf-8")
    out_json = tmp_path / "out.json"
    out_txt = tmp_path / "out.txt"
    monkeypatch.setattr(
        ss_cli.sys, "argv",
        ["prog", "--json", str(in_json), "--txt", str(in_txt),
         "--output-json", str(out_json), "--output-txt", str(out_txt), "--stem", "x",
         "--llm-report-dir", str(tmp_path / "report")],
    )
    ss_cli.main()
    assert out_json.read_bytes() == in_json.read_bytes()
    assert out_txt.read_bytes() == in_txt.read_bytes()
