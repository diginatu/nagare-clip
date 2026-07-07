"""Integration: <overlay text="...">...</overlay> markers in _edits.txt are
written to the output JSON's `overlays` key without affecting `keep_intervals`."""

import json

import yaml

import nagare_clip.intervals.run as stage_run
from nagare_clip.config import get_effective_config
from nagare_clip.intervals.run import run_intervals


def _whisperx_with_silence():
    """One segment with a 3.9-second intra-segment silent gap.

    Word-gap silence detection (threshold 1.0s) will exclude (1.1, 5.0).
    """
    return {
        "duration": 10.0,
        "segments": [
            {
                "start": 0.0,
                "end": 5.6,
                "text": "あいうえ",
                "words": [
                    {"word": "あ", "start": 0.5, "end": 0.8},
                    {"word": "い", "start": 0.8, "end": 1.1},
                    {"word": "う", "start": 5.0, "end": 5.3},
                    {"word": "え", "start": 5.3, "end": 5.6},
                ],
            },
        ],
    }


def _config(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "intervals": {
                    "silence_threshold": 1.0,
                    "min_keep": 0.001,
                    "keep_pre_margin": 0.0,
                    "keep_post_margin": 0.0,
                }
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _run(monkeypatch, tmp_path, edits_text: str):
    json_path = tmp_path / "in.json"
    json_path.write_text(json.dumps(_whisperx_with_silence()), encoding="utf-8")
    edits_path = tmp_path / "in_edits.txt"
    edits_path.write_text(edits_text, encoding="utf-8")
    out_path = tmp_path / "out.json"
    cfg_path = _config(tmp_path)

    monkeypatch.setattr(stage_run.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(stage_run, "build_bunsetu_times", lambda *a, **k: [])
    cfg = get_effective_config(cfg_path, {})
    run_intervals(edits_path, json_path, out_path, cfg)
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_overlays_key_present_when_marker_used(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, 'あ<overlay text="Chapter 1">いう</overlay>え')
    assert "overlays" in out
    assert out["overlays"] == [{"start": 0.8, "end": 5.3, "text": "Chapter 1"}]


def test_overlays_key_absent_without_markers(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "あいうえ")
    assert "overlays" not in out


def test_overlay_does_not_affect_keep_intervals(monkeypatch, tmp_path):
    """Overlay wraps the silent gap; it must NOT force-keep audio. The gap
    (1.1, 5.0) is still excluded from keep_intervals."""
    out = _run(monkeypatch, tmp_path, 'あ<overlay text="X">いう</overlay>え')
    keep = out["keep_intervals"]
    # No keep interval should cover the (1.1, 5.0) gap
    for iv in keep:
        assert not (iv["start"] <= 1.5 and iv["end"] >= 4.5), (
            f"Overlay accidentally force-kept gap: {iv}"
        )
