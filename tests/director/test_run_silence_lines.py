"""What the director's own call shows for a wait between two lines.

The real numbers are PXL_20260426_090431216's lines 53-54: the 29.9 s wait the
whole "n~" feature exists for.
"""

from __future__ import annotations

import json

import nagare_clip.director.director_llm as dl
import nagare_clip.director.run as director_run
from nagare_clip.order import Segment

CFG = {"director": {"enabled": True, "prompt": "P", "max_retries": 0}}

# line 53 「その状態で」 ends at 501.897, line 54 starts at 531.779.
WORDS = [
    [("そ", 500.953, 501.5), ("の", 501.5, 501.897)],
    [("こ", 531.779, 532.5), ("の", 532.5, 533.0)],
    [("で", 564.787, 565.0), ("す", 565.0, 566.43)],
]


def _project(tmp_path, words=WORDS):
    edits = tmp_path / "a_edits.txt"
    edits.write_text("その状態で\nこの状態で\nで、あとは。\n", encoding="utf-8")
    segments = [
        {
            "start": ws[0][1],
            "end": ws[-1][2],
            "text": "x",
            "words": [{"word": w, "start": s, "end": e} for w, s, e in ws],
        }
        for ws in words
    ]
    js = tmp_path / "a.json"
    js.write_text(json.dumps({"segments": segments}), encoding="utf-8")
    return edits, js


def _gaps(tmp_path, start, end, description):
    path = tmp_path / "a_gaps.json"
    path.write_text(
        json.dumps(
            {"gaps": [{"start": start, "end": end, "frames": [], "description": description}]}
        ),
        encoding="utf-8",
    )
    return path


def _run(tmp_path, monkeypatch, cfg=CFG, gaps=None):
    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return '{"ops": []}'

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    edits, js = _project(tmp_path)
    director_run.run_director(edits, cfg, segment=Segment("a", None), json_path=js, gaps=gaps)
    return seen["user"]


def test_the_wait_between_two_lines_is_shown_as_its_own_line(tmp_path, monkeypatch):
    user = _run(tmp_path, monkeypatch)
    assert "    [silent 29.9s after line 1]" in user
    # ...and the line it follows no longer counts it a second time.
    assert "gap 29.9s" not in user


def test_the_gap_description_becomes_the_silence_lines_text(tmp_path, monkeypatch):
    # The very annotation that used to be printed above the line.
    gaps = _gaps(tmp_path, 505.0, 525.0, "デモが動く")
    user = _run(tmp_path, monkeypatch, gaps=gaps)
    assert "    [silent 29.9s after line 1: デモが動く]" in user
    assert "[silent gap:" not in user


def test_the_threshold_is_the_configured_one(tmp_path, monkeypatch):
    cfg = {"director": dict(CFG["director"], silence_line_min=40.0)}
    gaps = _gaps(tmp_path, 505.0, 525.0, "デモが動く")
    user = _run(tmp_path, monkeypatch, cfg=cfg, gaps=gaps)
    # Below the threshold the wait is the bracket's business again, and its
    # description goes back to being an annotation under its own line.
    assert "silent 29.9s" not in user
    assert "gap 29.9s" in user
    assert "    [silent gap: デモが動く]" in user


def test_the_silence_is_measured_from_the_words_not_the_segment_bounds(tmp_path, monkeypatch):
    # A segment whose last word ends well before the segment does: the drop
    # logic reads the words, so the director must see the words' silence.
    words = [
        [("そ", 500.953, 501.5), ("の", 501.5, 501.897)],
        [("こ", 531.779, 532.5)],
        [("で", 564.787, 566.43)],
    ]
    edits = tmp_path / "a_edits.txt"
    edits.write_text("その状態で\nこの状態で\nで、あとは。\n", encoding="utf-8")
    segments = [
        {
            "start": ws[0][1],
            "end": ws[-1][2] + 5.0,  # a segment bound that runs past its words
            "text": "x",
            "words": [{"word": w, "start": s, "end": e} for w, s, e in ws],
        }
        for ws in words
    ]
    js = tmp_path / "a.json"
    js.write_text(json.dumps({"segments": segments}), encoding="utf-8")
    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return '{"ops": []}'

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    director_run.run_director(edits, CFG, segment=Segment("a", None), json_path=js)
    assert "    [silent 29.9s after line 1]" in seen["user"]


def test_the_default_threshold_is_the_configured_one():
    # The key the pipeline actually reads, and the fallback used when it is
    # absent, must be the same number.
    from nagare_clip.config import get_effective_config

    cfg = get_effective_config(None, {})
    assert cfg["director"]["silence_line_min"] == 5.0
    assert director_run.silence_line_min(cfg["director"]) == 5.0
    assert director_run.silence_line_min({}) == 5.0
    # gap_context describes every silence at least this long, so each described
    # gap has a silence line to land in.
    assert cfg["director"]["silence_line_min"] >= cfg["gap_context"]["min_gap"]


def test_a_broken_threshold_falls_back_to_the_default():
    assert director_run.silence_line_min({"silence_line_min": -1}) == 5.0
    assert director_run.silence_line_min({"silence_line_min": "soon"}) == 5.0
    assert director_run.silence_line_min({"silence_line_min": True}) == 5.0
    assert director_run.silence_line_min({"silence_line_min": 0}) == 0.0
