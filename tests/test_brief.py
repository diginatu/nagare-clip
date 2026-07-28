"""project brief: rendering and prompt injection."""

from __future__ import annotations

import json

from nagare_clip.brief import BRIEF_HEADER, apply_brief, format_brief, load_previous_summary
from nagare_clip.config import DEFAULTS


def test_defaults_render_empty():
    assert format_brief(DEFAULTS["project"]) == ""
    assert format_brief(None) == ""
    assert format_brief({}) == ""


def test_whitespace_only_fields_render_empty():
    assert format_brief({"audience": "   ", "tone": "\n"}) == ""


def test_fields_render_in_order():
    brief = format_brief(
        {
            "tone": "punchy",
            "audience": "DIY viewers",
            "target_duration": "12 minutes",
            "purpose": "test the rig",
            "story_so_far": "episode 2 built it",
        }
    )
    assert brief.splitlines() == [
        BRIEF_HEADER,
        "- Audience: DIY viewers",
        "- Purpose: test the rig",
        "- Target duration: 12 minutes",
        "- Tone: punchy",
        "- Story so far: episode 2 built it",
    ]


def test_partial_brief_renders_only_set_fields():
    assert format_brief({"tone": "punchy"}).splitlines() == [BRIEF_HEADER, "- Tone: punchy"]


def test_previous_summary_is_read_from_file(tmp_path):
    prev = tmp_path / "summary.json"
    prev.write_text(json.dumps({"summary": "built the siphon rig", "parts": []}), encoding="utf-8")
    brief = format_brief({"previous_summary": str(prev)})
    assert brief.splitlines() == [
        BRIEF_HEADER,
        "- Previous video (this project continues it): built the siphon rig",
    ]


def test_previous_summary_missing_or_malformed_degrades(tmp_path):
    assert load_previous_summary(tmp_path / "nope.json") == ""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_previous_summary(bad) == ""
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"parts": []}), encoding="utf-8")
    assert load_previous_summary(empty) == ""
    # A dead path leaves the rest of the brief intact.
    assert format_brief(
        {"tone": "punchy", "previous_summary": str(tmp_path / "nope.json")}
    ).splitlines() == [BRIEF_HEADER, "- Tone: punchy"]


def test_apply_brief_is_identity_when_unset():
    stage = {"prompt": "base"}
    assert apply_brief(stage, {"project": DEFAULTS["project"]}) is stage
    assert apply_brief(stage, {}) is stage


def test_apply_brief_appends_without_mutating():
    stage = {"prompt": "base", "overall_prompt": "reduce", "temperature": 0.3}
    out = apply_brief(stage, {"project": {"tone": "punchy"}}, keys=("prompt", "overall_prompt"))
    assert stage == {"prompt": "base", "overall_prompt": "reduce", "temperature": 0.3}
    assert out["prompt"] == f"base\n\n{BRIEF_HEADER}\n- Tone: punchy"
    assert out["overall_prompt"] == f"reduce\n\n{BRIEF_HEADER}\n- Tone: punchy"
    assert out["temperature"] == 0.3


def test_apply_brief_with_empty_prompt_has_no_leading_blank_lines():
    out = apply_brief({}, {"project": {"tone": "punchy"}})
    assert out["prompt"] == f"{BRIEF_HEADER}\n- Tone: punchy"
