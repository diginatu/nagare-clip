"""publish/publish_llm: title candidates, description lead, chapter titles and
thumbnail copy sets, parsed leniently from one LLM call."""

from __future__ import annotations

import json

from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.publish.publish_llm import (
    ThumbLine,
    collect_overlay_texts,
    format_publish_context,
    generate_publish_copy,
    try_parse_publish_response,
)
from nagare_clip.summary.summarize import PartSummary, ProjectSummary

FULL = {
    "titles": ["水槽DIY 擬似オーバーフロー", "穴あけ不要の排水装置"],
    "lead": "前回作った装置を実際の水槽で試します。",
    "chapters": [{"index": 1, "title": "前回のおさらい"}, {"index": 2, "title": "取り付け"}],
    "thumbnail_copy": [
        {
            "lines": [
                {"role": "tag", "text": "水槽DIY"},
                {"role": "hook", "text": "水浸し！"},
            ]
        },
        {"lines": [{"role": "hook", "text": "穴あけ不要。"}]},
    ],
}


def _ps() -> ProjectSummary:
    return ProjectSummary(
        summary="装置を水槽に取り付けてテストする回。",
        parts=[
            PartSummary("talk1", (1, 12), "前回の装置を振り返る", start=0.0, end=60.0),
            PartSummary("talk1", (13, 40), "水槽に取り付ける", start=60.0, end=300.0),
        ],
        video_summaries={"talk1": "装置の取り付けとテスト"},
    )


# --- parsing ---------------------------------------------------------------


def test_full_response_round_trips():
    copy = try_parse_publish_response(json.dumps(FULL), num_parts=2)
    assert copy is not None
    assert copy.titles == FULL["titles"]
    assert copy.lead == "前回作った装置を実際の水槽で試します。"
    assert copy.chapter_titles == {1: "前回のおさらい", 2: "取り付け"}
    assert copy.thumbnail_copy == [
        [ThumbLine("tag", "水槽DIY"), ThumbLine("hook", "水浸し！")],
        [ThumbLine("hook", "穴あけ不要。")],
    ]


def test_fenced_json_is_accepted():
    fenced = "```json\n" + json.dumps(FULL) + "\n```"
    copy = try_parse_publish_response(fenced, num_parts=2)
    assert copy is not None and copy.titles == FULL["titles"]


def test_invalid_json_is_a_hard_failure():
    assert try_parse_publish_response("not json", num_parts=2) is None


def test_missing_titles_is_a_hard_failure():
    """Several title candidates are the point of the call; one attempt that
    forgot them is worth retrying rather than degrading."""
    assert try_parse_publish_response(json.dumps({"lead": "x"}), num_parts=2) is None
    assert try_parse_publish_response(json.dumps({"titles": []}), num_parts=2) is None


def test_blank_and_duplicate_titles_are_dropped():
    data = {"titles": ["A", "  ", "A", "B", 7]}
    copy = try_parse_publish_response(json.dumps(data), num_parts=2)
    assert copy is not None and copy.titles == ["A", "B"]


def test_out_of_range_chapter_index_is_dropped():
    drops: list[str] = []
    data = {"titles": ["A"], "chapters": [{"index": 9, "title": "ghost"}]}
    copy = try_parse_publish_response(json.dumps(data), num_parts=2, drops=drops)
    assert copy is not None and copy.chapter_titles == {}
    assert drops


def test_thumbnail_set_of_one_to_three_lines_is_kept_verbatim():
    data = {
        "titles": ["A"],
        "thumbnail_copy": [
            {"lines": [{"role": "hook", "text": "one"}]},
            {
                "lines": [
                    {"role": "tag", "text": "t"},
                    {"role": "hook", "text": "h"},
                    {"role": "subtitle", "text": "s"},
                ]
            },
        ],
    }
    copy = try_parse_publish_response(json.dumps(data), num_parts=2)
    assert copy is not None
    assert [len(s) for s in copy.thumbnail_copy] == [1, 3]


def test_thumbnail_set_is_never_padded_to_three_lines():
    data = {"titles": ["A"], "thumbnail_copy": [{"lines": [{"role": "hook", "text": "one"}]}]}
    copy = try_parse_publish_response(json.dumps(data), num_parts=2)
    assert copy is not None and copy.thumbnail_copy == [[ThumbLine("hook", "one")]]


def test_unknown_role_drops_only_that_line():
    data = {
        "titles": ["A"],
        "thumbnail_copy": [
            {"lines": [{"role": "shout", "text": "x"}, {"role": "HOOK", "text": "h"}]}
        ],
    }
    drops: list[str] = []
    copy = try_parse_publish_response(json.dumps(data), num_parts=2, drops=drops)
    assert copy is not None and copy.thumbnail_copy == [[ThumbLine("hook", "h")]]
    assert drops


def test_thumbnail_set_longer_than_three_lines_is_trimmed():
    data = {
        "titles": ["A"],
        "thumbnail_copy": [
            {
                "lines": [
                    {"role": "tag", "text": "1"},
                    {"role": "hook", "text": "2"},
                    {"role": "subtitle", "text": "3"},
                    {"role": "subtitle", "text": "4"},
                ]
            }
        ],
    }
    drops: list[str] = []
    copy = try_parse_publish_response(json.dumps(data), num_parts=2, drops=drops)
    assert copy is not None
    assert [line.text for line in copy.thumbnail_copy[0]] == ["1", "2", "3"]
    assert drops


def test_empty_thumbnail_set_is_dropped():
    data = {"titles": ["A"], "thumbnail_copy": [{"lines": []}, "nonsense"]}
    copy = try_parse_publish_response(json.dumps(data), num_parts=2)
    assert copy is not None and copy.thumbnail_copy == []


# --- context ---------------------------------------------------------------


def test_context_carries_summaries_parts_and_overlays():
    doc = format_publish_context(
        _ps(),
        directions=[PartDirection("talk1", (13, 40), "feature this")],
        overlay_texts=["水浸し！", "呼び水、完成！"],
    )
    assert "装置を水槽に取り付けてテストする回。" in doc
    assert '"talk1"' in doc
    assert "1: talk1 [1-12] — 前回の装置を振り返る" in doc
    assert "feature this" in doc
    assert "水浸し！" in doc


def test_context_without_optional_material_still_lists_the_parts():
    doc = format_publish_context(_ps(), directions=None, overlay_texts=None)
    assert "2: talk1 [13-40] — 水槽に取り付ける" in doc
    assert "feature" not in doc


def test_collect_overlay_texts_takes_overlay_and_timelapse_captions():
    from nagare_clip.director.director_llm import DirectorOp

    ops = [
        DirectorOp(type="overlay", lines=(1, 1), text="水浸し！", duration=3.0),
        DirectorOp(type="timelapse", lines=(2, 4), factor=8.0, text="配管作業"),
        DirectorOp(type="cut", lines=(5, 6)),
        DirectorOp(type="overlay", lines=(7, 7), text="水浸し！", duration=3.0),
    ]
    assert collect_overlay_texts(ops) == ["水浸し！", "配管作業"]


# --- generation ------------------------------------------------------------


def test_generate_returns_the_parsed_copy():
    def fake_call(messages, cfg):
        assert messages[0]["role"] == "system"
        return json.dumps(FULL)

    copy = generate_publish_copy(_ps(), {"prompt": "P"}, call_llm=fake_call)
    assert copy.titles == FULL["titles"]


def test_generate_retries_an_unparseable_response():
    calls = {"n": 0}

    def fake_call(messages, cfg):
        calls["n"] += 1
        return "junk" if calls["n"] == 1 else json.dumps(FULL)

    copy = generate_publish_copy(_ps(), {"prompt": "P", "max_retries": 1}, call_llm=fake_call)
    assert calls["n"] == 2
    assert copy.titles == FULL["titles"]


def test_generate_degrades_to_empty_copy_when_every_attempt_fails():
    def fake_call(messages, cfg):
        raise RuntimeError("connection refused")

    copy = generate_publish_copy(_ps(), {"prompt": "P", "max_retries": 1}, call_llm=fake_call)
    assert copy.titles == [] and copy.lead == "" and copy.thumbnail_copy == []


def test_generate_without_parts_makes_no_call():
    called = {"n": 0}

    def fake_call(messages, cfg):
        called["n"] += 1
        return json.dumps(FULL)

    copy = generate_publish_copy(ProjectSummary("", []), {"prompt": "P"}, call_llm=fake_call)
    assert called["n"] == 0
    assert copy.titles == []
