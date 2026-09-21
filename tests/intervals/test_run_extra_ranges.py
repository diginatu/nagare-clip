"""Integration: run_intervals takes the time ranges resolved from ops that
address a silence, alongside the ones it extracts from text markers.

A `"n~"` op has no words to wrap, so it can never arrive as a marker; without
this path the silence it names stays dropped, which is exactly the failure the
whole feature exists to fix.
"""

import json

import yaml

import nagare_clip.intervals.run as stage_run
from nagare_clip.config import get_effective_config
from nagare_clip.intervals.op_times import OpTimes
from nagare_clip.intervals.run import run_intervals


def _whisperx_with_a_wait():
    """Two lines with a 3.9-second wait between them (1.1 → 5.0)."""
    return {
        "duration": 10.0,
        "segments": [
            {
                "start": 0.5,
                "end": 1.1,
                "text": "あい",
                "words": [
                    {"word": "あ", "start": 0.5, "end": 0.8},
                    {"word": "い", "start": 0.8, "end": 1.1},
                ],
            },
            {
                "start": 5.0,
                "end": 5.6,
                "text": "うえ",
                "words": [
                    {"word": "う", "start": 5.0, "end": 5.3},
                    {"word": "え", "start": 5.3, "end": 5.6},
                ],
            },
        ],
    }


def _setup(tmp_path):
    json_path = tmp_path / "clip.json"
    json_path.write_text(json.dumps(_whisperx_with_a_wait()), encoding="utf-8")
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "intervals": {
                    "silence_threshold": 1.0,
                    "min_keep": 0.001,
                    "keep_pre_margin": 0.0,
                    "keep_post_margin": 0.0,
                    "min_cut": 0.0,
                }
            }
        ),
        encoding="utf-8",
    )
    return json_path, edits, cfg


def _run(monkeypatch, tmp_path, extra=None):
    json_path, edits, cfg_path = _setup(tmp_path)
    monkeypatch.setattr(stage_run.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(stage_run, "build_bunsetu_times", lambda *a, **k: [])
    monkeypatch.setattr(stage_run, "bunsetu_join_text", lambda text, nlp, sep: text)
    out = tmp_path / "intervals.json"
    cfg = get_effective_config(cfg_path, {})
    if extra is None:
        run_intervals(edits, json_path, out, cfg)
    else:
        run_intervals(edits, json_path, out, cfg, extra=extra)
    return json.loads(out.read_text(encoding="utf-8"))


def _covers(intervals, t):
    return any(iv["start"] <= t <= iv["end"] for iv in intervals)


def test_without_extra_ranges_the_wait_is_dropped(tmp_path, monkeypatch):
    # The control: nothing in _edits.txt can name this silence.
    data = _run(monkeypatch, tmp_path)
    assert not _covers(data["keep_intervals"], 2.5)


def test_an_extra_keep_restores_the_wait(tmp_path, monkeypatch):
    data = _run(monkeypatch, tmp_path, extra=OpTimes(keeps=[(1.1, 5.0)]))
    assert _covers(data["keep_intervals"], 2.5)


def test_an_extra_speed_reaches_the_output(tmp_path, monkeypatch):
    data = _run(
        monkeypatch,
        tmp_path,
        extra=OpTimes(keeps=[(1.1, 5.0)], speeds=[(1.1, 5.0, 5.0)]),
    )
    assert {"start": 1.1, "end": 5.0, "factor": 5.0} in data["speed_ranges"]


def test_an_extra_overlay_reaches_the_output(tmp_path, monkeypatch):
    data = _run(
        monkeypatch,
        tmp_path,
        extra=OpTimes(keeps=[(1.1, 5.0)], overlays=[(1.1, 0.8, "待ち")]),
    )
    assert any(c.get("text") == "待ち" for c in data.get("overlays", []))


def test_extra_none_is_exactly_todays_behaviour(tmp_path, monkeypatch):
    # Passing the keyword explicitly as None must not differ from omitting it.
    without = _run(monkeypatch, tmp_path)
    explicit = _run(monkeypatch, tmp_path, extra=OpTimes())
    assert without == explicit


def test_marker_ranges_and_extra_ranges_both_apply(tmp_path, monkeypatch):
    # A <keep> in the text and a resolved range must union, not replace: the
    # two sources address different parts of the same source.
    # The marker has to rescue something that would otherwise be gone, or this
    # test passes even when the resolved ranges REPLACE the marker ones: plain
    # speech survives on its own, so only a _cuts.txt range it overrides makes
    # the marker's contribution visible.
    json_path, edits, cfg_path = _setup(tmp_path)
    edits.write_text("<keep>あい</keep>\nうえ\n", encoding="utf-8")
    cuts = tmp_path / "clip_cuts.txt"
    cuts.write_text("0.5 - 1.1\n", encoding="utf-8")
    monkeypatch.setattr(stage_run.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(stage_run, "build_bunsetu_times", lambda *a, **k: [])
    monkeypatch.setattr(stage_run, "bunsetu_join_text", lambda text, nlp, sep: text)
    out = tmp_path / "intervals.json"
    run_intervals(
        edits,
        json_path,
        out,
        get_effective_config(cfg_path, {}),
        cuts_txt=cuts,
        extra=OpTimes(keeps=[(1.1, 5.0)]),
    )
    keep = json.loads(out.read_text(encoding="utf-8"))["keep_intervals"]
    assert _covers(keep, 0.9)  # from the marker
    assert _covers(keep, 2.5)  # from the resolved range
