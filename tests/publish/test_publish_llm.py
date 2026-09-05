"""publish/publish_llm: title candidates, description lead, chapter titles and
thumbnail copy sets, parsed leniently from one LLM call."""

from __future__ import annotations

import json

from nagare_clip.plan.plan_llm import PartDirection
from nagare_clip.publish.publish_llm import (
    PublishCopy,
    ThumbLine,
    ThumbSet,
    format_publish_context,
    generate_publish_copy,
    thumbnail_copy_to_dict,
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
        ThumbSet(lines=[ThumbLine("tag", "水槽DIY"), ThumbLine("hook", "水浸し！")], style={}),
        ThumbSet(lines=[ThumbLine("hook", "穴あけ不要。")], style={}),
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
    assert [len(s.lines) for s in copy.thumbnail_copy] == [1, 3]


def test_thumbnail_set_is_never_padded_to_three_lines():
    data = {"titles": ["A"], "thumbnail_copy": [{"lines": [{"role": "hook", "text": "one"}]}]}
    copy = try_parse_publish_response(json.dumps(data), num_parts=2)
    assert copy is not None
    assert copy.thumbnail_copy == [ThumbSet(lines=[ThumbLine("hook", "one")], style={})]


def test_unknown_role_drops_only_that_line():
    data = {
        "titles": ["A"],
        "thumbnail_copy": [
            {"lines": [{"role": "shout", "text": "x"}, {"role": "HOOK", "text": "h"}]}
        ],
    }
    drops: list[str] = []
    copy = try_parse_publish_response(json.dumps(data), num_parts=2, drops=drops)
    assert copy is not None
    assert copy.thumbnail_copy == [ThumbSet(lines=[ThumbLine("hook", "h")], style={})]
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
    assert [line.text for line in copy.thumbnail_copy[0].lines] == ["1", "2", "3"]
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


# --- thumbnail style ---------------------------------------------------------


def test_no_style_key_at_all_reaches_the_artifact():
    """The copy call is blind: a style key here -- operator-shaped or not --
    is something it was not asked for."""
    data = {
        "titles": ["A"],
        "thumbnail_copy": [
            {"lines": [{"role": "hook", "text": "h", "-write": "/tmp/x"}], "-delete": "0"}
        ],
    }
    copy = try_parse_publish_response(json.dumps(data), num_parts=1)
    assert copy is not None
    assert copy.thumbnail_copy[0].lines[0].style == {}
    assert copy.thumbnail_copy[0].style == {}


def test_a_set_with_no_style_still_parses():
    data = {"titles": ["A"], "thumbnail_copy": [{"lines": [{"role": "hook", "text": "h"}]}]}
    copy = try_parse_publish_response(json.dumps(data), num_parts=1)
    assert copy is not None
    assert copy.thumbnail_copy == [ThumbSet(lines=[ThumbLine("hook", "h")], style={})]


def test_thumbnail_copy_to_dict_flattens_style_beside_the_text():
    copy = PublishCopy(
        titles=["A"],
        thumbnail_copy=[
            ThumbSet(
                lines=[ThumbLine("hook", "h", {"fill": "#B08D3E", "pointsize": 156})],
                style={"gravity": "southwest"},
            )
        ],
    )
    assert thumbnail_copy_to_dict(copy) == [
        {
            "lines": [{"role": "hook", "text": "h", "fill": "#B08D3E", "pointsize": 156}],
            "background": "",
            "gravity": "southwest",
        }
    ]


def test_the_prompts_own_thumbnail_example_parses():
    """A stale example in the prompt must fail loudly, not quietly mislead."""
    from nagare_clip.config import DEFAULTS

    prompt = DEFAULTS["publish"]["prompt"]
    start = prompt.index("{\n")
    shape = prompt[start : prompt.index("\n}\n", start) + 3]
    copy = try_parse_publish_response(shape, num_parts=3)
    assert copy is not None
    assert copy.thumbnail_copy and copy.thumbnail_copy[0].lines


def test_the_prompt_tells_the_model_the_sets_must_look_different():
    from nagare_clip.config import DEFAULTS

    assert "differ" in DEFAULTS["publish"]["prompt"]


def test_the_system_prompt_is_unchanged_when_no_fonts_are_configured(monkeypatch):
    """Regression guard: a project without font slots gets the prompt it had."""
    seen = []

    def fake_call(messages, cfg):
        seen.append(messages[0]["content"])
        return json.dumps({"titles": ["A"]})

    project = ProjectSummary("s", [PartSummary("a", (1, 2), "p")])
    generate_publish_copy(project, {"prompt": "BASE"}, call_llm=fake_call)
    generate_publish_copy(
        project, {"prompt": "BASE", "thumbnail": {"fonts": {}}}, call_llm=fake_call
    )
    assert seen == ["BASE", "BASE"]


def test_the_background_field_is_visible_in_publish_json():
    """The hand-edit loop needs somewhere obvious to type a path, so the key
    is written even when nothing chose one yet."""
    copy = PublishCopy(titles=["t"], thumbnail_copy=[ThumbSet(lines=[ThumbLine("hook", "H")])])
    assert thumbnail_copy_to_dict(copy)[0]["background"] == ""


def test_a_chosen_background_round_trips_through_publish_json():
    from nagare_clip.render.thumbnail import sets_from_dict

    copy = PublishCopy(
        titles=["t"],
        thumbnail_copy=[ThumbSet(lines=[ThumbLine("hook", "H")], background="frames/b/55.660.jpg")],
    )
    data = {"thumbnail_copy": thumbnail_copy_to_dict(copy)}
    assert sets_from_dict(data)[0].background == "frames/b/55.660.jpg"


# --- the copy call comes out blind -------------------------------------------


def test_the_copy_call_emits_role_and_text_only():
    """The look is the pairing call's job. A style key in this response is
    something the model was not asked for and must not reach publish.json."""
    response = json.dumps(
        {
            "titles": ["A"],
            "thumbnail_copy": [
                {
                    "lines": [{"role": "hook", "text": "h", "fill": "#B08D3E", "pointsize": 156}],
                    "gravity": "southwest",
                    "offset": "+56+62",
                }
            ],
        }
    )
    copy = try_parse_publish_response(response, num_parts=1)
    assert copy.thumbnail_copy[0].lines[0].style == {}
    assert copy.thumbnail_copy[0].style == {}


def test_the_copy_prompt_carries_no_look_vocabulary():
    """Anchoring works both ways: colour words in this prompt are what made a
    call that has never seen the picture choose a corner anyway."""
    from nagare_clip.config import get_effective_config

    prompt = get_effective_config(None, {})["publish"]["prompt"].lower()
    for word in ("fill", "stroke", "pointsize", "gravity", "offset", "imagemagick", "#rrggbb"):
        assert word not in prompt, f"{word!r} still in the copy prompt"


def test_the_copy_call_is_never_told_about_the_font_slots(monkeypatch):
    """Fonts are part of the look, so they belong to the pairing call."""
    seen = {}

    def fake(messages, cfg):
        seen["system"] = messages[0]["content"]
        return json.dumps({"titles": ["A"]})

    cfg = {"prompt": "BASE", "fonts": {"sans-bold": "Noto"}}
    generate_publish_copy(_ps(), cfg, call_llm=fake)
    assert seen["system"] == "BASE"


def test_no_frame_material_can_reach_the_copy_call():
    """The whole reason the pairing call is separate: 24 frame descriptions in
    front of this one and it starts captioning photographs."""
    context = format_publish_context(_ps())
    assert "frame" not in context.lower()
