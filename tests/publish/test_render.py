"""publish.json contract + the reviewable publish.md."""

from __future__ import annotations

from nagare_clip.publish.chapters import Chapter
from nagare_clip.publish.publish_llm import PublishCopy, ThumbLine, ThumbSet
from nagare_clip.publish.render import (
    build_description,
    empty_publish_data,
    publish_to_dict,
    render_markdown,
)
from nagare_clip.publish.thumbs import ThumbCandidate

CHAPTERS = [Chapter(0.0, "はじめに", "a", 1), Chapter(95.0, "取り付け", "a", 2)]
COPY = PublishCopy(
    titles=["title one", "title two"],
    lead="what this video shows.",
    thumbnail_copy=[ThumbSet([ThumbLine("tag", "水槽DIY"), ThumbLine("hook", "穴あけ不要。")])],
)
FRAMES = [ThumbCandidate("a", 12.5, 8.0, "overlay: 水浸し！")]


def test_description_is_lead_then_timestamp_list():
    assert build_description("lead.", CHAPTERS) == "lead.\n\n0:00 はじめに\n1:35 取り付け"
    assert build_description("", CHAPTERS) == "0:00 はじめに\n1:35 取り付け"
    assert build_description("lead.", []) == "lead."


def test_publish_dict_shape():
    data = publish_to_dict(COPY, CHAPTERS, [], FRAMES, 600.0)
    assert data["titles"] == ["title one", "title two"]
    assert data["chapters"][1] == {
        "time": "1:35",
        "seconds": 95.0,
        "title": "取り付け",
        "stem": "a",
        "part_index": 2,
    }
    assert data["thumbnail_copy"] == [
        {"lines": [{"role": "tag", "text": "水槽DIY"}, {"role": "hook", "text": "穴あけ不要。"}]}
    ]
    assert data["thumbnail_frames"] == [
        {
            "path": "frames/a/12.500.jpg",
            "stem": "a",
            "source_time": 12.5,
            "timeline_time": 8.0,
            "reason": "overlay: 水浸し！",
        }
    ]
    assert data["duration_sec"] == 600.0
    assert set(data) == set(empty_publish_data())


def test_markdown_holds_every_section_and_a_paste_ready_description():
    md = render_markdown(publish_to_dict(COPY, CHAPTERS, [], FRAMES, 600.0))
    assert "1. title one" in md
    assert "```\nwhat this video shows.\n\n0:00 はじめに\n1:35 取り付け\n```" in md
    assert "tag: 水槽DIY / hook: 穴あけ不要。" in md
    assert "`frames/a/12.500.jpg`" in md
    assert "YouTube will not render" not in md


def test_markdown_warns_but_still_prints_a_list_that_cannot_qualify():
    issues = ["only 2 timestamp(s); YouTube needs at least 3"]
    md = render_markdown(publish_to_dict(COPY, CHAPTERS, issues, [], 600.0))
    assert "YouTube will not render these timestamps as chapters" in md
    assert "> - only 2 timestamp(s); YouTube needs at least 3" in md
    # the list is still there — YouTube auto-links timestamps regardless
    assert "0:00 はじめに" in md


def test_disabled_markdown_says_so():
    md = render_markdown(empty_publish_data(), enabled=False)
    assert "publish stage disabled" in md
