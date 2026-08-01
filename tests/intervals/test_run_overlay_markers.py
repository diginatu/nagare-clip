"""Integration: `<overlay text="..." duration="N.N"/>` markers in _edits.txt are
written to the output JSON's `overlays` key without affecting `keep_intervals`."""

import json

import yaml

import nagare_clip.intervals.run as stage_run
from nagare_clip.audio_silence.cuts_file import write_cuts
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
    monkeypatch.setattr(stage_run, "bunsetu_join_text", lambda text, nlp, sep: text)
    cfg = get_effective_config(cfg_path, {})
    run_intervals(edits_path, json_path, out_path, cfg)
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_overlays_key_present_when_marker_used(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, 'あ<overlay text="Chapter 1" duration="3.0"/>いうえ')
    assert "overlays" in out
    assert out["overlays"] == [{"start": 0.8, "duration": 3.0, "text": "Chapter 1"}]


def test_overlays_key_absent_without_markers(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "あいうえ")
    assert "overlays" not in out


def test_overlay_does_not_affect_keep_intervals(monkeypatch, tmp_path):
    """The overlay sits right before the silent gap; it must NOT force-keep
    audio. The gap (1.1, 5.0) is still excluded from keep_intervals."""
    out = _run(monkeypatch, tmp_path, 'あ<overlay text="X" duration="6.0"/>いうえ')
    keep = out["keep_intervals"]
    # No keep interval should cover the (1.1, 5.0) gap
    for iv in keep:
        assert not (iv["start"] <= 1.5 and iv["end"] >= 4.5), (
            f"Overlay accidentally force-kept gap: {iv}"
        )


def _whisperx_with_cut_opening():
    """The water_pump_3 shape: one line 2.495-18.751 whose opening seconds are
    cut by the audio_silence list, leaving 4.488-12.638 as the only keep."""
    words = []
    t = 2.495
    while round(t + 0.5, 3) <= 12.638:
        words.append({"word": "あ", "start": round(t, 3), "end": round(t + 0.5, 3)})
        t += 0.5
    words.append({"word": "あ", "start": round(t, 3), "end": 12.638})
    return {
        "duration": 18.751,
        "segments": [
            {
                "start": 2.495,
                "end": 18.751,
                "text": "あ" * len(words),
                "words": words,
            },
        ],
    }


def _run_cut_opening(monkeypatch, tmp_path, margins: dict | None = None):
    data = _whisperx_with_cut_opening()
    json_path = tmp_path / "in.json"
    json_path.write_text(json.dumps(data), encoding="utf-8")
    edits_path = tmp_path / "in_edits.txt"
    line = "あ" * len(data["segments"][0]["words"])
    edits_path.write_text(
        f'<overlay text="前回のおさらい" duration="5.0"/>{line}', encoding="utf-8"
    )
    cuts_path = tmp_path / "in_cuts.txt"
    write_cuts(cuts_path, [(0.0, 4.488)])
    out_path = tmp_path / "out.json"

    cfg_path = tmp_path / "config.yml"
    ivl = {
        "silence_threshold": 1.0,
        "min_keep": 0.001,
        "keep_pre_margin": 0.0,
        "keep_post_margin": 0.0,
    }
    ivl.update(margins or {})
    cfg_path.write_text(yaml.safe_dump({"intervals": ivl}), encoding="utf-8")

    monkeypatch.setattr(stage_run.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(stage_run, "build_bunsetu_times", lambda *a, **k: [])
    monkeypatch.setattr(stage_run, "bunsetu_join_text", lambda text, nlp, sep: text)
    cfg = get_effective_config(cfg_path, {})
    run_intervals(edits_path, json_path, out_path, cfg, cuts_txt=cuts_path)
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_overlay_on_a_cut_line_opening_snaps_to_the_first_surviving_moment(monkeypatch, tmp_path):
    """The anchor lands 1.99s inside cut footage; the caption moves to the
    start of the line's first keep interval instead of being dropped later."""
    out = _run_cut_opening(monkeypatch, tmp_path)

    assert out["keep_intervals"] == [{"start": 4.488, "end": 12.638}]
    assert out["overlays"] == [
        {"start": 4.488, "duration": 5.0, "text": "前回のおさらい"},
    ]


def test_snapped_anchor_lands_inside_a_written_keep_interval(monkeypatch, tmp_path):
    """The blender stage looks the anchor up in the keep intervals *as written*,
    with half-open containment — so a snapped start must survive the rounding
    both values go through, margins and all."""
    out = _run_cut_opening(
        # 4.488 - 0.1239 = 4.3641: a keep start whose 4th decimal rounds *down*,
        # so an unrounded interval would leave the rounded anchor just outside it.
        monkeypatch,
        tmp_path,
        {"keep_pre_margin": 0.1239, "keep_post_margin": 0.0567},
    )

    start = out["overlays"][0]["start"]
    assert any(iv["start"] <= start < iv["end"] for iv in out["keep_intervals"]), (
        f"anchor {start} is on no keep interval: {out['keep_intervals']}"
    )


def test_director_duration_survives_guided_edit_into_intervals(monkeypatch, tmp_path):
    """End-to-end: the number the director chose is the number the blender
    stage is handed — no stage in between re-derives it from a tag position."""
    import nagare_clip.guided_edit.run as ge_run

    edits = tmp_path / "in_edits.txt"
    edits.write_text("あいうえ\n", encoding="utf-8")
    director = tmp_path / "in_director.json"
    director.write_text(
        json.dumps(
            {"ops": [{"type": "overlay", "lines": [1, 1], "text": "水位差", "duration": 4.0}]}
        ),
        encoding="utf-8",
    )
    guided = tmp_path / "guided_edits.txt"
    ge_run.run_guided_edit(
        edits_txt=edits,
        director_json=director,
        output=guided,
        cfg={"guided_edit": {"enabled": True}},
    )
    assert (
        guided.read_text(encoding="utf-8")
        .splitlines()[0]
        .startswith('<overlay text="水位差" duration="4.0"/>')
    )

    out = _run(monkeypatch, tmp_path, guided.read_text(encoding="utf-8"))
    assert out["overlays"] == [{"start": 0.5, "duration": 4.0, "text": "水位差"}]
