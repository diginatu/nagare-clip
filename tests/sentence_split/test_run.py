import copy
import inspect
import json

from nagare_clip.sentence_split import run as ss_run
from nagare_clip.sentence_split.segment import concat_word_text, segment_from_words


def _seg(text, t0, step=1.0):
    """A segment of one-character words, each ``step`` seconds long."""
    return {
        "start": float(t0),
        "end": float(t0 + step * len(text)),
        "text": text,
        "words": [
            {
                "word": ch,
                "start": float(t0 + i * step),
                "end": float(t0 + (i + 1) * step),
                "score": 1.0,
            }
            for i, ch in enumerate(text)
        ],
    }


def _char_bunsetsu(monkeypatch):
    # One bunsetsu per character: deterministic, no GiNZA model needed.
    monkeypatch.setattr(
        ss_run, "bunsetsu_units", lambda text, nlp: [(i, i + 1, ch) for i, ch in enumerate(text)]
    )


class FakeLLM:
    """Counts calls and answers with a fixed response (or raises)."""

    def __init__(self, response='{"sentences":[[0,2],[3,9]]}', error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, messages, cfg):
        self.calls.append(messages[1]["content"])
        if self.error:
            raise self.error
        return self.response(messages) if callable(self.response) else self.response


def _cfg(**kw):
    return {"enabled": True, "max_line_seconds": 8.0, "max_retries": 0, **kw}


def _split(data, llm, **cfg):
    return ss_run.split_long_lines(data, _cfg(**cfg), get_nlp=lambda: None, stem="x", call_llm=llm)


# --- 1. short lines are untouched and never sent ------------------------------


def test_short_lines_untouched_and_no_llm_call(monkeypatch):
    _char_bunsetsu(monkeypatch)
    data = {"language": "ja", "segments": [_seg("あいうえお", 0), _seg("かきく", 6)]}
    before = copy.deepcopy(data)
    llm = FakeLLM()
    out = _split(data, llm)
    assert llm.calls == []
    assert out["segments"][0] is data["segments"][0]
    assert out["segments"][1] is data["segments"][1]
    assert out == before


# --- 2. threshold boundary ------------------------------------------------------


def test_duration_equal_to_threshold_is_not_split(monkeypatch):
    _char_bunsetsu(monkeypatch)
    data = {"language": "ja", "segments": [_seg("あいうえおかきくけこ", 0, step=0.8)]}  # 8.0s
    llm = FakeLLM()
    out = _split(data, llm)
    assert llm.calls == []
    assert [s["text"] for s in out["segments"]] == ["あいうえおかきくけこ"]


def test_duration_just_over_threshold_is_split(monkeypatch):
    _char_bunsetsu(monkeypatch)
    seg = _seg("あいうえおかきくけこ", 0, step=0.8)
    seg["end"] = 8.01
    data = {"language": "ja", "segments": [seg]}
    llm = FakeLLM()
    out = _split(data, llm)
    assert len(llm.calls) == 1
    assert [s["text"] for s in out["segments"]] == ["あいう", "えおかきくけこ"]


def test_threshold_comes_from_config(monkeypatch):
    _char_bunsetsu(monkeypatch)
    data = {"language": "ja", "segments": [_seg("あいうえおかきくけこ", 0)]}  # 10s
    llm = FakeLLM()
    out = _split(data, llm, max_line_seconds=12.0)
    assert llm.calls == []
    assert out["segments"][0] is data["segments"][0]


# --- 3. a long line is split into pieces of its own words ------------------------


def test_long_line_split_keeps_words_order_and_neighbours(monkeypatch):
    _char_bunsetsu(monkeypatch)
    before, long_, after = _seg("さし", 0), _seg("あいうえおかきくけこ", 3), _seg("すせ", 14)
    data = {"language": "ja", "segments": [before, long_, after]}
    llm = FakeLLM()
    out = _split(data, llm)
    texts = [s["text"] for s in out["segments"]]
    assert texts == ["さし", "あいう", "えおかきくけこ", "すせ"]
    assert out["segments"][0] is before
    assert out["segments"][-1] is after
    # the pieces hold the original word objects, in order
    piece_words = out["segments"][1]["words"] + out["segments"][2]["words"]
    assert len(piece_words) == len(long_["words"])
    assert all(a is b for a, b in zip(piece_words, long_["words"]))
    assert out["segments"][1]["start"] == 3.0 and out["segments"][1]["end"] == 6.0
    assert out["segments"][2]["start"] == 6.0 and out["segments"][2]["end"] == 13.0
    assert concat_word_text(out["segments"]) == concat_word_text(data["segments"])
    assert out["word_segments"] == [w for s in out["segments"] for w in s["words"]]
    # the LLM saw only the long line, numbered by bunsetsu
    assert llm.calls == [" ".join(f"{i}:{ch}" for i, ch in enumerate("あいうえおかきくけこ"))]


# --- 4. failures keep the line ---------------------------------------------------


def test_llm_failure_keeps_line(monkeypatch):
    _char_bunsetsu(monkeypatch)
    data = {"language": "ja", "segments": [_seg("あいうえおかきくけこ", 0)]}
    llm = FakeLLM(error=RuntimeError("no server"))
    out = _split(data, llm, max_retries=1)
    assert len(llm.calls) == 2
    assert out["segments"][0] is data["segments"][0]


def test_invalid_ranges_keep_line(monkeypatch):
    _char_bunsetsu(monkeypatch)
    data = {"language": "ja", "segments": [_seg("あいうえおかきくけこ", 0)]}
    llm = FakeLLM(response='{"sentences":[[0,2],[4,9]]}')  # gap at 3
    out = _split(data, llm)
    assert out["segments"] == data["segments"]
    assert out["segments"][0] is data["segments"][0]


def test_single_range_keeps_line(monkeypatch):
    _char_bunsetsu(monkeypatch)
    one, two = _seg("あいうえおかきくけこ", 0), _seg("たちつてとなにぬねの", 11)
    data = {"language": "ja", "segments": [one, two]}

    def answer(messages):
        if messages[1]["content"].startswith("0:あ"):
            return '{"sentences":[[0,9]]}'  # one sentence: keep the line
        return '{"sentences":[[0,2],[3,9]]}'

    llm = FakeLLM(response=answer)
    out = _split(data, llm)
    assert len(llm.calls) == 2
    assert out["segments"][0] is one
    assert [s["text"] for s in out["segments"]] == [
        "あいうえおかきくけこ",
        "たちつ",
        "てとなにぬねの",
    ]


def test_verbatim_mismatch_keeps_line(monkeypatch):
    _char_bunsetsu(monkeypatch)
    data = {"language": "ja", "segments": [_seg("あいうえおかきくけこ", 0)]}
    # a rebuild that loses a word must never reach the output
    monkeypatch.setattr(
        ss_run,
        "rebuild_segments",
        lambda words, bunsetsu, ranges, c2w: [
            segment_from_words(words[:3]),
            segment_from_words(words[4:]),
        ],
    )
    out = _split(data, FakeLLM())
    assert out["segments"][0] is data["segments"][0]


# --- 5. fewer than two bunsetsu ---------------------------------------------------


def test_long_line_with_one_bunsetsu_not_sent(monkeypatch):
    monkeypatch.setattr(ss_run, "bunsetsu_units", lambda text, nlp: [(0, len(text), text)])
    data = {"language": "ja", "segments": [_seg("あいうえおかきくけこ", 0)]}
    llm = FakeLLM()
    out = _split(data, llm)
    assert llm.calls == []
    assert out["segments"][0] is data["segments"][0]


# --- 6. untimed segments -----------------------------------------------------------


def test_segment_without_numeric_times_untouched(monkeypatch):
    _char_bunsetsu(monkeypatch)
    no_start = _seg("あいうえおかきくけこ", 0)
    del no_start["start"]
    str_end = _seg("あいうえおかきくけこ", 0)
    str_end["end"] = "99"
    data = {"language": "ja", "segments": [no_start, str_end]}
    llm = FakeLLM()
    out = _split(data, llm)
    assert llm.calls == []
    assert out["segments"][0] is no_start and out["segments"][1] is str_end


# --- 8. several long lines ------------------------------------------------------------


def test_each_long_line_gets_one_call_in_order(monkeypatch):
    _char_bunsetsu(monkeypatch)
    data = {
        "language": "ja",
        "segments": [
            _seg("あいうえおかきくけこ", 0),
            _seg("さし", 11),
            _seg("たちつてとなにぬねの", 14),
        ],
    }
    llm = FakeLLM()
    out = _split(data, llm)
    assert [c.split(" ")[0] for c in llm.calls] == ["0:あ", "0:た"]
    assert [s["text"] for s in out["segments"]] == [
        "あいう",
        "えおかきくけこ",
        "さし",
        "たちつ",
        "てとなにぬねの",
    ]


def test_recorder_unit_per_long_line(monkeypatch):
    _char_bunsetsu(monkeypatch)
    units = []

    class Rec:
        stage = "sentence_split"

        def begin(self, unit):
            units.append(unit)

        def attempt(self, **kw):
            pass

        def flush_unit(self, unit, **kw):
            pass

    data = {
        "language": "ja",
        "segments": [
            _seg("さし", 0),
            _seg("あいうえおかきくけこ", 3),
            _seg("たちつてとなにぬねの", 14),
        ],
    }
    ss_run.split_long_lines(
        data, _cfg(), get_nlp=lambda: None, stem="x", call_llm=FakeLLM(), recorder=Rec()
    )
    assert units == ["x line 2", "x line 3"]


# --- run_sentence_split -------------------------------------------------------------


def _write_inputs(tmp_path, data, txt):
    in_json = tmp_path / "in.json"
    in_txt = tmp_path / "in.txt"
    in_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    in_txt.write_text(txt, encoding="utf-8")
    return in_json, in_txt


def test_run_writes_split_json_and_txt(tmp_path, monkeypatch):
    data = {"language": "ja", "segments": [_seg("さし", 0), _seg("あいうえおかきくけこ", 3)]}
    in_json, in_txt = _write_inputs(tmp_path, data, "さし\nあいうえおかきくけこ\n")
    out_json, out_txt = tmp_path / "out.json", tmp_path / "out.txt"
    monkeypatch.setattr(ss_run, "load_nlp", lambda: None)
    _char_bunsetsu(monkeypatch)
    monkeypatch.setattr(
        ss_run, "split_line", lambda bunsetsu, cfg, **kw: [(0, 2), (3, len(bunsetsu) - 1)]
    )
    ss_run.run_sentence_split(
        in_json, in_txt, out_json, out_txt, {"sentence_split": _cfg()}, stem="x"
    )
    new = json.loads(out_json.read_text(encoding="utf-8"))
    assert [s["text"] for s in new["segments"]] == ["さし", "あいう", "えおかきくけこ"]
    assert out_txt.read_text(encoding="utf-8") == "さし\nあいう\nえおかきくけこ\n"


def test_run_with_no_long_line_writes_equal_content_without_nlp(tmp_path, monkeypatch):
    data = {"language": "ja", "segments": [_seg("さし", 0), _seg("あい", 3)]}
    in_json, in_txt = _write_inputs(tmp_path, data, "さし\nあい\n")
    out_json, out_txt = tmp_path / "out.json", tmp_path / "out.txt"

    def no_nlp():
        raise AssertionError("GiNZA must not load when no line is long")

    monkeypatch.setattr(ss_run, "load_nlp", no_nlp)
    ss_run.run_sentence_split(
        in_json, in_txt, out_json, out_txt, {"sentence_split": _cfg()}, stem="x"
    )
    assert json.loads(out_json.read_text(encoding="utf-8")) == data
    assert out_txt.read_text(encoding="utf-8") == "さし\nあい\n"


def test_run_enabled_speechless_source_is_byte_identical(tmp_path, monkeypatch):
    data = {"language": "ja", "segments": [], "word_segments": []}
    in_json, in_txt = _write_inputs(tmp_path, data, "")
    out_json, out_txt = tmp_path / "out.json", tmp_path / "out.txt"
    monkeypatch.setattr(ss_run, "load_nlp", lambda: None)
    ss_run.run_sentence_split(
        in_json, in_txt, out_json, out_txt, {"sentence_split": _cfg()}, stem="x"
    )
    assert out_txt.read_text(encoding="utf-8") == ""
    assert out_json.read_bytes() == in_json.read_bytes()


def test_run_enabled_no_long_line_is_byte_identical(tmp_path, monkeypatch):
    data = {"language": "ja", "segments": [_seg("さし", 0), _seg("あい", 3)]}
    in_json, in_txt = _write_inputs(tmp_path, data, "")
    # compact JSON and a txt without the trailing newline: re-serialising would differ
    in_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    in_txt.write_text("さし\nあい", encoding="utf-8")
    out_json, out_txt = tmp_path / "out.json", tmp_path / "out.txt"
    monkeypatch.setattr(ss_run, "load_nlp", lambda: None)
    ss_run.run_sentence_split(
        in_json, in_txt, out_json, out_txt, {"sentence_split": _cfg()}, stem="x"
    )
    assert out_json.read_bytes() == in_json.read_bytes()
    assert out_txt.read_bytes() == in_txt.read_bytes()


def test_run_takes_no_cuts_txt():
    assert "cuts_txt" not in inspect.signature(ss_run.run_sentence_split).parameters


def test_disabled_copy_through_byte_identical(tmp_path):
    data = {"language": "ja", "segments": [_seg("あいうえおかきくけこ", 0)]}
    in_json, in_txt = _write_inputs(tmp_path, data, "あいうえおかきくけこ\n")
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
