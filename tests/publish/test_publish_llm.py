"""publish LLM: prompt context, lenient parsing, retry/degrade."""

from __future__ import annotations

import json

from nagare_clip.config import DEFAULTS
from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.publish.chapters import Chapter
from nagare_clip.publish.publish_llm import (
    PublishCopy,
    counts_note,
    format_publish_context,
    generate_publish_copy,
    try_parse_publish_response,
)
from nagare_clip.summary.summarize import PartSummary, ProjectSummary

CHAPTERS = [Chapter(0.0, "one", "a", 1), Chapter(90.0, "two", "a", 2)]


def _response(**over):
    data = {
        "titles": ["t1", "t2"],
        "lead": "a lead",
        "chapters": [{"index": 1, "title": "はじめに"}],
        "thumbnail_copy": [{"lines": [{"role": "hook", "text": "水浸し！"}]}],
    }
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


# --- parsing ----------------------------------------------------------------


def test_parses_every_field():
    copy = try_parse_publish_response(_response(), num_chapters=2)
    assert copy.titles == ["t1", "t2"]
    assert copy.lead == "a lead"
    assert copy.chapter_titles == {1: "はじめに"}
    assert [(ln.role, ln.text) for ln in copy.thumbnail_copy[0].lines] == [("hook", "水浸し！")]


def test_fenced_response_is_accepted():
    assert try_parse_publish_response(f"```json\n{_response()}\n```", 2).titles == ["t1", "t2"]


def test_invalid_json_or_missing_titles_is_a_hard_failure():
    assert try_parse_publish_response("not json", 2) is None
    assert try_parse_publish_response('{"lead": "x"}', 2) is None


def test_out_of_range_and_malformed_chapter_titles_are_dropped():
    drops: list[str] = []
    copy = try_parse_publish_response(
        _response(chapters=[{"index": 9, "title": "x"}, {"index": 2, "title": ""}, "junk"]),
        num_chapters=2,
        drops=drops,
    )
    assert copy.chapter_titles == {}
    assert len(drops) == 2


def test_thumbnail_set_is_capped_at_three_lines_and_unknown_roles_become_hooks():
    drops: list[str] = []
    copy = try_parse_publish_response(
        _response(
            thumbnail_copy=[
                {
                    "lines": [
                        {"role": "tag", "text": "水槽DIY"},
                        {"role": "shout", "text": "穴あけ不要。"},
                        {"text": "擬似オーバーフロー"},
                        {"role": "hook", "text": "too many"},
                    ]
                },
                {"lines": []},
            ]
        ),
        num_chapters=2,
        drops=drops,
    )
    assert len(copy.thumbnail_copy) == 1
    assert [(ln.role, ln.text) for ln in copy.thumbnail_copy[0].lines] == [
        ("tag", "水槽DIY"),
        ("hook", "穴あけ不要。"),
        ("hook", "擬似オーバーフロー"),
    ]
    assert any("truncated" in d for d in drops)


def test_a_single_hook_set_is_valid():
    copy = try_parse_publish_response(
        _response(thumbnail_copy=[{"lines": [{"role": "hook", "text": "呼び水、完成！"}]}]), 2
    )
    assert len(copy.thumbnail_copy[0].lines) == 1


def test_wrong_typed_fields_degrade_instead_of_failing():
    drops: list[str] = []
    copy = try_parse_publish_response(
        _response(lead=5, chapters="nope", thumbnail_copy={}), num_chapters=2, drops=drops
    )
    assert copy.titles == ["t1", "t2"]
    assert copy.lead == "" and copy.chapter_titles == {} and copy.thumbnail_copy == []
    assert len(drops) == 3


# --- context ----------------------------------------------------------------


def _project():
    return ProjectSummary(
        summary="all about pumps",
        parts=[
            PartSummary("a", (1, 10), "builds the siphon"),
            PartSummary("b", (1, 5), "tests it"),
        ],
        video_summaries={"a": "part one of the build"},
    )


def test_context_carries_summaries_plan_overlays_and_final_chapters():
    text = format_publish_context(
        _project(),
        CHAPTERS,
        directions=[PartDirection("b", (1, 5), "feature — the payoff")],
        overlays=["水浸し！"],
        duration=605.0,
    )
    assert "Finished video length: 10:05" in text
    assert "Overall: all about pumps" in text
    assert 'Video "a": part one of the build' in text
    assert "2: [1-5] tests it — plan: feature — the payoff" in text
    assert "- 水浸し！" in text
    assert "1: 0:00 — one" in text and "2: 1:30 — two" in text


def test_context_omits_absent_sections():
    text = format_publish_context(ProjectSummary(summary=""), [])
    assert text == ""


def test_counts_note_states_the_configured_numbers():
    assert "Give 4 title candidate(s)" in counts_note(4, 2)
    assert "2 alternative thumbnail copy set(s)" in counts_note(4, 2)


# --- generation -------------------------------------------------------------


def _cfg(**over):
    cfg = dict(DEFAULTS["publish"])
    cfg.update(over)
    return cfg


def test_counts_note_is_appended_to_the_system_prompt():
    seen: dict = {}

    def fake(messages, cfg):
        seen["system"] = messages[0]["content"]
        return _response()

    generate_publish_copy(_project(), CHAPTERS, _cfg(title_count=7), call_llm=fake)
    assert DEFAULTS["publish"]["prompt"] in seen["system"]
    assert "Give 7 title candidate(s)" in seen["system"]


def test_retries_an_unparseable_response_then_succeeds():
    calls: list[int] = []

    def fake(messages, cfg):
        calls.append(1)
        return "garbage" if len(calls) == 1 else _response()

    copy = generate_publish_copy(_project(), CHAPTERS, _cfg(), call_llm=fake)
    assert len(calls) == 2
    assert copy.titles == ["t1", "t2"]


def test_all_attempts_failing_degrades_to_empty_copy():
    def fake(messages, cfg):
        raise ConnectionError("down")

    assert generate_publish_copy(_project(), CHAPTERS, _cfg(), call_llm=fake) == PublishCopy()
