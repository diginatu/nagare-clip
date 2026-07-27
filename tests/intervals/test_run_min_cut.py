"""Integration: intervals.min_cut absorbs cuts too short to be worth the jump."""

import json

import yaml

import nagare_clip.intervals.run as stage_run
from nagare_clip.config import get_effective_config
from nagare_clip.intervals.run import run_intervals


def _whisperx():
    return {
        "duration": 10.0,
        "segments": [
            {
                "start": 0.0,
                "end": 10.0,
                "text": "",
                "words": [
                    {"word": "a", "start": 0.0, "end": 0.5},
                    {"word": "b", "start": 9.5, "end": 10.0},
                ],
            }
        ],
    }


def _setup(tmp_path, min_cut=None):
    json_path = tmp_path / "clip.json"
    json_path.write_text(json.dumps(_whisperx()), encoding="utf-8")
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("", encoding="utf-8")
    intervals_cfg = {
        "silence_threshold": 1000.0,
        "min_keep": 0.001,
        "keep_pre_margin": 0.0,
        "keep_post_margin": 0.0,
    }
    if min_cut is not None:
        intervals_cfg["min_cut"] = min_cut
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump({"intervals": intervals_cfg}), encoding="utf-8")
    return json_path, edits, cfg


def _run(monkeypatch, json_path, edits, cfg_path, out, cuts=None):
    # Avoid loading the real ja_ginza model; segments have empty text so
    # build_bunsetu_times never invokes nlp.
    monkeypatch.setattr(stage_run.spacy, "load", lambda *a, **k: object())
    cfg = get_effective_config(cfg_path, {})
    run_intervals(edits, json_path, out, cfg, cuts_txt=cuts)
    return json.loads(out.read_text(encoding="utf-8"))


def _covers(intervals, t):
    return any(iv["start"] <= t <= iv["end"] for iv in intervals)


def test_short_cut_is_absorbed_by_default_min_cut(tmp_path, monkeypatch):
    json_path, edits, cfg = _setup(tmp_path)
    cuts = tmp_path / "clip_cuts.txt"
    cuts.write_text("5.000 - 5.200\n", encoding="utf-8")  # 0.2s cut, below min_cut

    out = tmp_path / "intervals.json"
    data = _run(monkeypatch, json_path, edits, cfg, out, cuts=cuts)

    assert data["keep_intervals"] == [{"start": 0.0, "end": 10.0}]


def test_short_cut_survives_when_min_cut_is_zero(tmp_path, monkeypatch):
    json_path, edits, cfg = _setup(tmp_path, min_cut=0.0)
    cuts = tmp_path / "clip_cuts.txt"
    cuts.write_text("5.000 - 5.200\n", encoding="utf-8")

    out = tmp_path / "intervals.json"
    data = _run(monkeypatch, json_path, edits, cfg, out, cuts=cuts)

    assert len(data["keep_intervals"]) == 2
    assert not _covers(data["keep_intervals"], 5.1)


def test_cut_above_min_cut_is_left_alone(tmp_path, monkeypatch):
    json_path, edits, cfg = _setup(tmp_path)
    cuts = tmp_path / "clip_cuts.txt"
    cuts.write_text("5.000 - 6.000\n", encoding="utf-8")  # 1.0s cut, above min_cut

    out = tmp_path / "intervals.json"
    data = _run(monkeypatch, json_path, edits, cfg, out, cuts=cuts)

    assert len(data["keep_intervals"]) == 2
    assert not _covers(data["keep_intervals"], 5.5)
