"""build_publish: a validated edit -> today's publish.json shape."""

from __future__ import annotations

from nagare_clip.publish.document import empty_publish, render_publish_markdown
from nagare_clip.publish.timeline import Placement
from nagare_clip.publish.tools.build import build_publish
from nagare_clip.publish.tools.edit import ChapterRef, PublishEdit, ResolvedChapter
from nagare_clip.render.thumbnail import sets_from_dict

PL = [Placement("a", 0.0, 60.0, 0.0, 60.0, 1.0)]
THUMBS = [
    {
        "background": "frames/a/1.000.jpg",
        "lines": [{"text": "hello", "role": "hook"}, {"text": "sub"}],
    }
]
STILLS = [
    {"stem": "a", "source_time": 5.0, "kind": "payoff", "label": "x", "line": 3, "path": "p.jpg"}
]


def _edit(thumbnails=THUMBS):
    return PublishEdit(["t1"], "lead", [ChapterRef(1, "Open")], thumbnails)


def _chapters(*pairs):
    return [ResolvedChapter(i + 1, t, f"c{i}") for i, t in enumerate(pairs)]


def test_build_shape_matches_today():
    data = build_publish(_edit(), _chapters(3.2), PL, STILLS)
    assert set(data) == set(empty_publish())
    assert data["titles"] == ["t1"]
    assert data["thumbnails"][0]["timeline_time"] == 5.0
    assert data["thumbnails"][0]["line"] == 3
    assert data["thumbnails"][0]["path"] == "p.jpg"


def test_first_chapter_forced_to_zero():
    data = build_publish(_edit(), _chapters(3.2, 20.0, 40.0), PL, [])
    assert data["chapters"][0] == {"time": 3.2, "timestamp": "0:00", "title": "c0"}
    assert data["chapters"][1]["timestamp"] == "0:20"
    assert data["description"] == "lead\n\n0:00 c0\n0:20 c1\n0:40 c2"
    assert data["chapters_qualify"] is True


def test_chapter_issues_when_fewer_than_three():
    data = build_publish(_edit(), _chapters(3.2, 30.0), PL, [])
    assert data["chapters_qualify"] is False
    assert any("3" in issue for issue in data["chapter_issues"])


def test_no_placements_gives_null_timeline_time():
    data = build_publish(_edit(), [], None, STILLS)
    assert data["thumbnails"][0]["timeline_time"] is None


def test_thumbnail_copy_round_trips_through_render():
    data = build_publish(_edit(), [], PL, [])
    (s,) = sets_from_dict(data)
    assert [line.text for line in s.lines] == ["hello", "sub"]
    assert s.lines[1].role == "hook"
    assert s.background == "frames/a/1.000.jpg"


def test_markdown_renders():
    data = build_publish(_edit(), _chapters(3.2), PL, STILLS)
    md = render_publish_markdown(data, "html")
    assert "## Title candidates" in md and "1. t1" in md and "- hook: hello" in md


def test_a_set_line_with_no_role_has_no_empty_prefix():
    data = empty_publish()
    data["thumbnail_copy"] = [
        {"lines": [{"role": "", "text": "Hi"}, {"role": "hook", "text": "Yo"}]}
    ]
    md = render_publish_markdown(data, "html")
    assert "- Hi" in md and "- hook: Yo" in md
    assert "- : " not in md
