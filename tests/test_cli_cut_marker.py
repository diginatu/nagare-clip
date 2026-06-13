"""Integration: <cut>...</cut> markers in _edits.txt delete the wrapped text;
a large enough deletion then falls out of the timeline because the gap between
the surviving neighbouring words exceeds the word-gap silence threshold."""

import json
import sys

import yaml

import nagare_clip.cli as stage_cli


def _whisperx_three_segments():
    """Three segments with small inter-segment gaps (< threshold), so with no
    edits everything is kept.  Deleting the middle segment opens a 1.4s gap
    (1.1 -> 2.5) that exceeds the 1.0s threshold and gets cut."""
    return {
        "duration": 5.0,
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
                "start": 1.3,
                "end": 1.9,
                "text": "うえ",
                "words": [
                    {"word": "う", "start": 1.3, "end": 1.6},
                    {"word": "え", "start": 1.6, "end": 1.9},
                ],
            },
            {
                "start": 2.5,
                "end": 3.1,
                "text": "おか",
                "words": [
                    {"word": "お", "start": 2.5, "end": 2.8},
                    {"word": "か", "start": 2.8, "end": 3.1},
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


def _run(monkeypatch, tmp_path, edits_text):
    json_path = tmp_path / "clip.json"
    json_path.write_text(json.dumps(_whisperx_three_segments()), encoding="utf-8")
    edits = tmp_path / "clip_edits.txt"
    edits.write_text(edits_text, encoding="utf-8")
    cfg = _config(tmp_path)
    out = tmp_path / "intervals.json"
    monkeypatch.setattr(stage_cli.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(stage_cli, "build_bunsetu_times", lambda *a, **k: [])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nagare_clip.cli",
            "--edits-txt",
            str(edits),
            "--json",
            str(json_path),
            "--config",
            str(cfg),
            "--output",
            str(out),
        ],
    )
    stage_cli.main()
    return json.loads(out.read_text(encoding="utf-8"))


def _covers(intervals, t):
    return any(iv["start"] <= t <= iv["end"] for iv in intervals)


def test_no_cut_keeps_everything(tmp_path, monkeypatch):
    """Control: with no <cut>, the small inter-segment gaps are all kept."""
    data = _run(monkeypatch, tmp_path, "あい\nうえ\nおか\n")
    keep = data["keep_intervals"]
    assert _covers(keep, 1.7)  # middle segment region
    assert _covers(keep, 0.9)
    assert _covers(keep, 2.7)


def test_cut_middle_segment_drops_it_from_timeline(tmp_path, monkeypatch):
    """`<cut>うえ</cut>` deletes the middle segment; the resulting 1.4s gap
    exceeds the threshold so the region is cut from keep_intervals, while the
    neighbouring segments survive."""
    data = _run(monkeypatch, tmp_path, "あい\n<cut>うえ</cut>\nおか\n")
    keep = data["keep_intervals"]
    # The deleted middle region must NOT be kept
    assert not _covers(keep, 1.7)
    # Neighbours survive
    assert _covers(keep, 0.9)
    assert _covers(keep, 2.7)


def test_cross_line_cut_drops_spanned_region(tmp_path, monkeypatch):
    """A <cut> opening in seg0 and closing in seg2 removes the spanned words;
    only the unwrapped head ('あ') and tail ('か') survive."""
    data = _run(monkeypatch, tmp_path, "あ<cut>い\nうえ\nお</cut>か\n")
    keep = data["keep_intervals"]
    # 'あ' (0.5-0.8) and 'か' (2.8-3.1) survive; everything between is cut
    assert _covers(keep, 0.7)
    assert _covers(keep, 2.9)
    assert not _covers(keep, 1.7)
