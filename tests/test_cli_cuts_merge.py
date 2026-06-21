"""Integration: --cuts-txt ranges are unioned into the interval excludes."""

import json
import sys

import yaml

import nagare_clip.intervals.cli as stage_cli


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


def _setup(tmp_path):
    json_path = tmp_path / "clip.json"
    json_path.write_text(json.dumps(_whisperx()), encoding="utf-8")
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("", encoding="utf-8")
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "intervals": {
                    "silence_threshold": 1000.0,
                    "min_keep": 0.001,
                    "keep_pre_margin": 0.0,
                    "keep_post_margin": 0.0,
                }
            }
        ),
        encoding="utf-8",
    )
    return json_path, edits, cfg


def _run(monkeypatch, json_path, edits, cfg, out, cuts=None):
    # Avoid loading the real ja_ginza model; segments have empty text so
    # build_bunsetu_times never invokes nlp.
    monkeypatch.setattr(stage_cli.spacy, "load", lambda *a, **k: object())
    argv = [
        "nagare_clip.intervals.cli",
        "--edits-txt",
        str(edits),
        "--json",
        str(json_path),
        "--config",
        str(cfg),
        "--output",
        str(out),
    ]
    if cuts is not None:
        argv += ["--cuts-txt", str(cuts)]
    monkeypatch.setattr(sys, "argv", argv)
    stage_cli.main()
    return json.loads(out.read_text(encoding="utf-8"))


def _covers(intervals, t):
    return any(iv["start"] <= t <= iv["end"] for iv in intervals)


def test_cuts_txt_excludes_span_from_keep_intervals(tmp_path, monkeypatch):
    json_path, edits, cfg = _setup(tmp_path)
    cuts = tmp_path / "clip_cuts.txt"
    cuts.write_text("3.000 - 6.000\n", encoding="utf-8")

    out = tmp_path / "intervals.json"
    data = _run(monkeypatch, json_path, edits, cfg, out, cuts=cuts)

    keep = data["keep_intervals"]
    assert not _covers(keep, 4.5)
    assert _covers(keep, 1.0)
    assert _covers(keep, 8.0)


def test_without_cuts_txt_span_is_kept(tmp_path, monkeypatch):
    json_path, edits, cfg = _setup(tmp_path)
    out = tmp_path / "intervals.json"
    data = _run(monkeypatch, json_path, edits, cfg, out, cuts=None)

    assert _covers(data["keep_intervals"], 4.5)
