"""Integration: <keep>...</keep> markers in _edits.txt force-keep the
underlying time range, carving it out of both word-gap silence and _cuts.txt."""

import json
import sys

import yaml

import nagare_clip.cli as stage_cli


def _whisperx_with_silence():
    """One segment with a 3.9-second intra-segment silent gap between 'い' and 'う'.

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
    """Stage 4 config with aggressive silence detection and zero margins so
    behavior is easy to assert."""
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


def _setup(tmp_path, edits_text):
    json_path = tmp_path / "clip.json"
    json_path.write_text(json.dumps(_whisperx_with_silence()), encoding="utf-8")
    edits = tmp_path / "clip_edits.txt"
    edits.write_text(edits_text, encoding="utf-8")
    return json_path, edits, _config(tmp_path)


def _run(monkeypatch, json_path, edits, cfg, out, cuts=None):
    monkeypatch.setattr(stage_cli.spacy, "load", lambda *a, **k: object())
    # Bypass GiNZA bunsetsu parsing — captions aren't asserted in these tests.
    monkeypatch.setattr(stage_cli, "build_bunsetu_times", lambda *a, **k: [])
    argv = [
        "nagare_clip.cli",
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


def test_keep_marker_carves_word_gap_silence(tmp_path, monkeypatch):
    """`<keep>いう</keep>` wraps words spanning the silent gap (1.1 → 5.0).

    The force-keep range (0.8, 5.3) carves the silence out of the excludes,
    so the gap is preserved in keep_intervals."""
    json_path, edits, cfg = _setup(tmp_path, "あ<keep>いう</keep>え\n")
    out = tmp_path / "intervals.json"
    data = _run(monkeypatch, json_path, edits, cfg, out)
    keep = data["keep_intervals"]
    # Mid-gap (2.5) lies inside the silence range (1.1, 5.0) — must survive
    assert _covers(keep, 2.5)
    # And the wrapped words themselves
    assert _covers(keep, 1.0)
    assert _covers(keep, 5.15)


def test_keep_marker_overrides_cuts_txt(tmp_path, monkeypatch):
    """A <keep> block must survive even when a _cuts.txt range covers it."""
    json_path, edits, cfg = _setup(tmp_path, "あ<keep>い</keep>うえ\n")
    cuts = tmp_path / "clip_cuts.txt"
    # _cuts.txt range fully covers 'い' (0.8-1.1)
    cuts.write_text("0.8 - 1.1\n", encoding="utf-8")

    out = tmp_path / "intervals.json"
    data = _run(monkeypatch, json_path, edits, cfg, out, cuts=cuts)
    keep = data["keep_intervals"]
    # The <keep> region must survive the cuts.txt range
    assert _covers(keep, 0.95)


def test_no_keep_marker_silence_still_cut(tmp_path, monkeypatch):
    """Control: without <keep>, the word-gap silence (1.1, 5.0) is excluded."""
    json_path, edits, cfg = _setup(tmp_path, "あいうえ\n")
    out = tmp_path / "intervals.json"
    data = _run(monkeypatch, json_path, edits, cfg, out)
    keep = data["keep_intervals"]
    # Mid-gap (2.5) must NOT be kept
    assert not _covers(keep, 2.5)


def test_keep_marker_with_internal_patch(tmp_path, monkeypatch):
    """<keep>{{えー->}}う</keep> — patch deletes 'えー', force-keep covers post-patch text."""
    # Use a single segment where the patched-away text is the only thing inside <keep>
    # so the post-patch visible content is just 'う'.
    data_json = {
        "duration": 10.0,
        "segments": [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "あえーう",
                "words": [
                    {"word": "あ", "start": 0.5, "end": 0.8},
                    {"word": "え", "start": 0.8, "end": 1.0},
                    {"word": "ー", "start": 1.0, "end": 1.2},
                    {"word": "う", "start": 1.8, "end": 2.0},
                ],
            },
        ],
    }
    json_path = tmp_path / "clip.json"
    json_path.write_text(json.dumps(data_json), encoding="utf-8")
    edits = tmp_path / "clip_edits.txt"
    # After patch, segment text is "あう". <keep>{{えー->}}う</keep> wraps an empty
    # post-patch start plus the literal 'う' → time range is (start of 'う', end of 'う').
    edits.write_text("あ<keep>{{えー->}}う</keep>\n", encoding="utf-8")
    cfg = _config(tmp_path)
    out = tmp_path / "intervals.json"
    data = _run(monkeypatch, json_path, edits, cfg, out)
    keep = data["keep_intervals"]
    # Word 'う' (1.8-2.0) must be in keep set
    assert _covers(keep, 1.9)
