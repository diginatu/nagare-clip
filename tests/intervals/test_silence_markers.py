"""Markers on silence lines: a tag there resolves to the silence's own edges.

An opener on the silence line after ``n`` starts where that silence starts
(line ``n``'s last speech span ends); a closer ends where it ends (line
``n+1``'s first speech span starts).  :func:`_oracle_span_bounds` is a frozen
copy of the pre-change ``intervals.op_times._span_bounds`` — the reference the
silence edges must reproduce.
"""

from __future__ import annotations

import json

import pytest
import yaml

import nagare_clip.intervals.run as stage_run
from nagare_clip.config import DEFAULTS, get_effective_config
from nagare_clip.edit_lines import gap_spans, insert_silence_lines
from nagare_clip.intervals.keep import dropped_ranges
from nagare_clip.intervals.run import run_intervals
from nagare_clip.intervals.speech import line_speech_spans
from nagare_clip.intervals.sync_json import (
    extract_cut_silences,
    extract_keep_ranges,
    extract_overlay_marks,
    extract_speed_ranges,
    sync_text_to_json,
)

from .silence_equivalence_cases import INTERVALS_CFG, TEXT_FILTER_LINES, WHISPERX

S = {n: f"[silent {e - s:.1f}s]" for n, (s, e) in gap_spans(WHISPERX).items() if e - s >= 5.0}
SIL = gap_spans(WHISPERX)


def _oracle_span_bounds(first, last, gap_start, gap_end, data=WHISPERX):
    """Frozen copy of op_times._span_bounds (pre-change reference)."""
    spans = line_speech_spans(data)
    start = spans[first - 1][-1][1] if gap_start else spans[first - 1][0][0]
    end = spans[last][0][0] if gap_end else spans[last - 1][-1][1]
    return (start, end)


def _file(**marks):
    """The equivalence fixture with every silence line, then *marks* applied.

    ``marks`` maps a physical line (1-based, as ``l<n>``) to its new text.
    """
    lines = insert_silence_lines(TEXT_FILTER_LINES, S)
    for key, text in marks.items():
        lines[int(key[1:]) - 1] = text
    return lines


# Physical layout: 1 あい, 2 S1, 3 うえ, 4 おか, 5 S3, 6 きく, 7 S4, 8 けこ, 9 さし
def test_the_fixture_layout():
    assert _file() == [
        "あい",
        "[silent 10.0s]",
        "うえ",
        "おか",
        "[silent 8.0s]",
        "きく",
        "[silent 6.0s]",
        "けこ",
        "さし",
    ]


def _keeps(lines):
    return extract_keep_ranges(lines, sync_text_to_json(WHISPERX, lines), silences=SIL)


@pytest.mark.parametrize(
    ("marks", "op"),
    [
        ({"l2": "<keep>[silent 10.0s]</keep>"}, (1, 1, True, True)),
        ({"l2": "<keep>[silent 10.0s]", "l4": "おか</keep>"}, (1, 3, True, False)),
        ({"l3": "<keep>うえ", "l5": "[silent 8.0s]</keep>"}, (2, 3, False, True)),
        ({"l2": "<keep>[silent 10.0s]", "l5": "[silent 8.0s]</keep>"}, (1, 3, True, True)),
        ({"l5": "<keep>[silent 8.0s]", "l7": "[silent 6.0s]</keep>"}, (3, 4, True, True)),
    ],
)
def test_keep_edges_on_silence_lines_match_the_oracle(marks, op):
    assert _keeps(_file(**marks)) == [_oracle_span_bounds(*op)]


def test_where_the_tag_sits_on_a_silence_line_does_not_matter():
    assert _keeps(_file(l2="[silent 10.0s]<keep></keep>")) == [SIL[1]]
    assert _keeps(_file(l2="</keep>[silent 10.0s]", l1="<keep>あい")) == [(0.5, SIL[1][1])]


def test_a_description_is_never_read_as_a_marker():
    # An escaped "]" (as silence_body writes one) stays inside the body.
    lines = _file(l2="[silent 10.0s: a <keep> sign</keep> \\] {{a->b}}]")
    assert _keeps(lines) == []
    assert sync_text_to_json(WHISPERX, lines) == sync_text_to_json(WHISPERX, _file())


def test_speed_on_silence_lines_resolves_like_keep():
    lines = _file(l2='<speed factor="8.0">[silent 10.0s]</speed>')
    synced = sync_text_to_json(WHISPERX, lines)
    assert extract_speed_ranges(lines, synced, silences=SIL) == [(*SIL[1], 8.0)]


def test_overlay_on_a_silence_line_starts_at_the_silence():
    lines = _file(l5='<overlay text="待ち" duration="2.0"/>[silent 8.0s]')
    synced = sync_text_to_json(WHISPERX, lines)
    assert extract_overlay_marks(lines, synced, silences=SIL) == [(SIL[3][0], 2.0, "待ち")]


def test_speech_line_tags_keep_todays_semantics_across_a_silence_line():
    # A <keep> closed at the START of line 2 falls back to line 1's last word,
    # skipping the silence line between them — exactly as it did without it.
    lines = _file(l1="<keep>あい", l3="</keep>うえ")
    assert _keeps(lines) == [(0.5, 1.1)]


def test_silence_lines_without_markers_change_nothing():
    legacy = TEXT_FILTER_LINES
    with_s = _file()
    assert sync_text_to_json(WHISPERX, with_s) == sync_text_to_json(WHISPERX, legacy)


# --- <cut> ----------------------------------------------------------------------


def _words(lines):
    synced = sync_text_to_json(WHISPERX, lines)
    return ["".join(w["word"] for w in seg["words"]) for seg in synced["segments"]]


def test_a_cut_from_a_silence_spares_the_line_before_it():
    # cut ["1~", 3]: the old marker path wrapped lines 1..3 whole and deleted
    # line 1's words too.
    lines = _file(l2="<cut>[silent 10.0s]", l4="おか</cut>")
    assert _words(lines) == ["あい", "", "", "きく", "けこ", "さし"]


def test_a_cut_on_one_silence_deletes_no_words():
    lines = _file(l2="<cut>[silent 10.0s]</cut>")
    assert _words(lines) == _words(_file())


def test_cut_silences_are_every_silence_a_cut_covers():
    lines = _file(l2="<cut>[silent 10.0s]", l4="おか", l5="[silent 8.0s]</cut>")
    assert extract_cut_silences(lines, SIL) == [SIL[1], SIL[3]]
    assert extract_cut_silences(_file(l4="<cut>おか</cut>"), SIL) == []
    assert extract_cut_silences(_file(l5="[silent 8.0s]<cut></cut>"), SIL) == [SIL[3]]
    # A cut opened on a speech line covers the silence lines inside it.
    assert extract_cut_silences(_file(l4="<cut>おか", l6="きく</cut>"), SIL) == [SIL[3]]


# --- run_intervals / dropped_ranges -----------------------------------------------


def _run(tmp_path, monkeypatch, lines, data=WHISPERX, extra_cfg=None):
    monkeypatch.setattr(stage_run.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(stage_run, "build_bunsetu_times", lambda *a, **k: [])
    monkeypatch.setattr(stage_run, "bunsetu_join_text", lambda text, nlp, sep: text)
    (tmp_path / "c.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "c_edits.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "config.yml").write_text(
        yaml.safe_dump({"intervals": INTERVALS_CFG, **(extra_cfg or {})}), encoding="utf-8"
    )
    cfg = get_effective_config(tmp_path / "config.yml", {})
    out = tmp_path / "c_intervals.json"
    run_intervals(tmp_path / "c_edits.txt", tmp_path / "c.json", out, cfg)
    return out.read_text(encoding="utf-8")


def test_run_intervals_output_is_identical_with_bare_silence_lines(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    legacy = _run(tmp_path / "a", monkeypatch, TEXT_FILTER_LINES)
    assert legacy == _run(tmp_path / "b", monkeypatch, _file())


def test_run_intervals_keeps_a_silence_kept_on_its_line(tmp_path, monkeypatch):
    out = json.loads(_run(tmp_path, monkeypatch, _file(l2="<keep>[silent 10.0s]</keep>")))
    assert {"start": 0.0, "end": 13.0} in out["keep_intervals"]


def test_run_intervals_drops_a_cut_silence_even_under_the_threshold(tmp_path, monkeypatch):
    # silence_threshold 20 s would keep every wait here; the cut still drops
    # the one it covers.
    lines = _file(l2="<cut>[silent 10.0s]</cut>")
    cfg = {"intervals": dict(INTERVALS_CFG, silence_threshold=20.0)}
    out = json.loads(_run(tmp_path, monkeypatch, lines, extra_cfg=cfg))
    assert not any(iv["start"] < 5.0 < iv["end"] for iv in out["keep_intervals"])


def test_run_intervals_refuses_a_deleted_silence_line(tmp_path, monkeypatch):
    lines = _file()
    del lines[4]  # the silence after speech line 3
    with pytest.raises(ValueError, match="missing silence line after speech line 3"):
        _run(tmp_path, monkeypatch, lines)


def test_run_intervals_uses_the_director_threshold_for_the_silence_set(tmp_path, monkeypatch):
    # With silence_line_min 7.0 the 6.0 s wait after line 4 has no line.
    lines = _file()
    with pytest.raises(ValueError, match="unexpected silence line after speech line 4"):
        _run(tmp_path, monkeypatch, lines, extra_cfg={"director": {"silence_line_min": 7.0}})


def _drops(lines):
    ivl = {**DEFAULTS["intervals"], **INTERVALS_CFG}
    return dropped_ranges(WHISPERX, ivl, edit_lines=lines, nlp=object())


def test_dropped_ranges_honour_a_keep_on_a_silence_line(monkeypatch):
    import nagare_clip.intervals.keep as keep_mod

    monkeypatch.setattr(keep_mod, "build_bunsetu_times", lambda *a, **k: [])
    assert (1.1, 11.1) in _drops(_file())
    assert (1.1, 11.1) not in _drops(_file(l2="<keep>[silent 10.0s]</keep>"))
    # #15 accounting: the text_filter shape (no silence lines) is unchanged.
    assert _drops(TEXT_FILTER_LINES) == _drops(_file())


def test_a_silence_keeps_its_times_when_the_line_before_it_is_cut(tmp_path, monkeypatch):
    # The silence after line 1 is resolved from the ORIGINAL timings: deleting
    # line 1's words must not make the <keep> on it unresolvable.
    lines = _file(l1="<cut>あい</cut>", l2="<keep>[silent 10.0s]</keep>")
    out = json.loads(_run(tmp_path, monkeypatch, lines))
    assert any(iv["start"] <= 5.0 <= iv["end"] for iv in out["keep_intervals"])


def test_a_stretched_final_word_is_where_the_speech_edges_differ():
    # Spec Q2: a SPEECH edge keeps its word semantics (the raw last word end),
    # where the deleted op_times used the clamped speech span.  They differ
    # only for a word stretched past SILENCE_MAX_WORD_SPAN (0.6 s).
    data = {
        "segments": [
            {"text": "あ", "words": [{"word": "あ", "start": 0.5, "end": 1.0}]},
            {"text": "い", "words": [{"word": "い", "start": 11.0, "end": 19.0}]},
            {"text": "う", "words": [{"word": "う", "start": 25.0, "end": 26.0}]},
        ]
    }
    silences = gap_spans(data)
    lines = ["あ", "<keep>[silent 10.0s]", "い</keep>", "[silent 13.4s]", "う"]
    got = extract_keep_ranges(lines, sync_text_to_json(data, lines), silences=silences)
    assert got == [(1.0, 19.0)]  # the raw end of the stretched word
    assert _oracle_span_bounds(1, 2, True, False, data) == (1.0, 11.6)  # the clamped one
