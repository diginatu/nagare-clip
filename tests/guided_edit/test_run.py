"""guided_edit.run: disabled passthrough and enabled apply paths."""

from __future__ import annotations

import json

import nagare_clip.guided_edit.run as ge_run


def test_disabled_copies_through(tmp_path):
    """When guided_edit.enabled is False, copy edits through unchanged."""
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あ\nい\n", encoding="utf-8")
    director = tmp_path / "clip_director.json"
    director.write_text(json.dumps({"ops": [{"type": "cut", "lines": [1, 1]}]}), encoding="utf-8")
    out = tmp_path / "out_edits.txt"

    cfg = {"guided_edit": {"enabled": False}}
    ge_run.run_guided_edit(
        edits_txt=edits,
        director_json=director,
        output=out,
        cfg=cfg,
    )

    assert out.read_text(encoding="utf-8") == "あ\nい\n"


def test_enabled_applies_ops(tmp_path):
    """When guided_edit.enabled is True, apply director ops."""
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あいう\nかきく\n", encoding="utf-8")
    director = tmp_path / "clip_director.json"
    director.write_text(json.dumps({"ops": [{"type": "cut", "lines": [1, 1]}]}), encoding="utf-8")
    out = tmp_path / "out_edits.txt"

    cfg = {"guided_edit": {"enabled": True}}
    ge_run.run_guided_edit(
        edits_txt=edits,
        director_json=director,
        output=out,
        cfg=cfg,
    )

    # cut is a span op -> applied deterministically (no LLM needed)
    assert out.read_text(encoding="utf-8").splitlines()[0] == "<cut>あいう</cut>"
    assert not (tmp_path / "out_unapplied.txt").exists()


def test_timelapse_op_becomes_nested_markers_with_a_spanning_caption(tmp_path):
    """One timelapse op produces <keep><speed><overlay/> on the first line and
    the closing tags on the last, with a caption lasting the whole timelapse."""
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あいう\nかきく\nさしす\n", encoding="utf-8")
    whisper = tmp_path / "clip.json"
    whisper.write_text(
        json.dumps(
            {
                "segments": [
                    {"text": "あいう", "start": 10.0, "end": 20.0},
                    {"text": "かきく", "start": 20.0, "end": 30.0},
                    {"text": "さしす", "start": 30.0, "end": 50.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    director = tmp_path / "clip_director.json"
    director.write_text(
        json.dumps(
            {"ops": [{"type": "timelapse", "lines": [1, 3], "factor": 4.0, "text": "配管作業"}]}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out_edits.txt"

    ge_run.run_guided_edit(
        edits_txt=edits,
        director_json=director,
        output=out,
        cfg={"guided_edit": {"enabled": True}},
        json_path=whisper,
    )

    written = out.read_text(encoding="utf-8").splitlines()
    # (50.0 - 10.0) / 4.0 = 10.0 seconds on the edited timeline.
    assert (
        written[0] == '<keep><speed factor="4.0"><overlay text="配管作業" duration="10.0"/>あいう'
    )
    assert written[1] == "かきく"
    assert written[2] == "さしす</speed></keep>"


def test_timelapse_output_passes_the_edits_checker(tmp_path):
    """The desugared file must be one the intervals stage accepts."""
    from nagare_clip.intervals.check_edits import check_edits

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あいう\nかきく\nさしす\n", encoding="utf-8")
    whisper_data = {
        "segments": [
            {"text": "あいう", "start": 10.0, "end": 20.0},
            {"text": "かきく", "start": 20.0, "end": 30.0},
            {"text": "さしす", "start": 30.0, "end": 50.0},
        ]
    }
    whisper = tmp_path / "clip.json"
    whisper.write_text(json.dumps(whisper_data), encoding="utf-8")
    director = tmp_path / "clip_director.json"
    director.write_text(
        json.dumps(
            {"ops": [{"type": "timelapse", "lines": [1, 3], "factor": 8.0, "text": "作業"}]}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out_edits.txt"

    ge_run.run_guided_edit(
        edits_txt=edits,
        director_json=director,
        output=out,
        cfg={"guided_edit": {"enabled": True}},
        json_path=whisper,
    )

    problems = check_edits(out.read_text(encoding="utf-8").splitlines(), whisper_data)
    assert problems == []


def test_timelapse_without_json_still_applies_speed_and_keep(tmp_path):
    """No segment times -> no caption, but the continuity fix still lands."""
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あいう\nかきく\n", encoding="utf-8")
    director = tmp_path / "clip_director.json"
    director.write_text(
        json.dumps(
            {"ops": [{"type": "timelapse", "lines": [1, 2], "factor": 4.0, "text": "作業"}]}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out_edits.txt"

    ge_run.run_guided_edit(
        edits_txt=edits,
        director_json=director,
        output=out,
        cfg={"guided_edit": {"enabled": True}},
    )

    written = out.read_text(encoding="utf-8").splitlines()
    assert written[0] == '<keep><speed factor="4.0">あいう'
    assert written[1] == "かきく</speed></keep>"
    assert "<overlay" not in "\n".join(written)
