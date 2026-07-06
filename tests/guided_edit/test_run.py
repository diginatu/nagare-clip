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


def test_enabled_applies_ops(monkeypatch, tmp_path):
    """When guided_edit.enabled is True, apply director ops."""

    def fake_apply_ops(lines, ops, cfg, recorder=None, unit=None):
        # For a cut op, apply it deterministically (no LLM)
        result = list(lines)
        for op in ops:
            if op.type == "cut" and op.lines:
                start, end = op.lines[0] - 1, op.lines[1] - 1
                for i in range(start, min(end + 1, len(result))):
                    if i < len(result):
                        result[i] = f"<cut>{result[i]}</cut>"
        return result, []

    monkeypatch.setattr(ge_run, "apply_ops", fake_apply_ops)

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
