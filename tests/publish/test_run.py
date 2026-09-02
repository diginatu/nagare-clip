"""publish run: disabled no-op, and the enabled path that turns summary parts
plus the finished timeline into title candidates, a description with chapters,
thumbnail copy and a still shortlist."""

from __future__ import annotations

import json

import nagare_clip.publish.run as publish_run
from nagare_clip.publish.publish_llm import PublishCopy, ThumbLine, ThumbSet
from nagare_clip.publish.thumbs import ThumbShot
from nagare_clip.summary.summarize import PartSummary, ProjectSummary, summary_to_dict

PARTS = [
    PartSummary("a", (1, 10), "前回のおさらい", start=0.0, end=100.0),
    PartSummary("a", (11, 20), "取り付け", start=100.0, end=300.0),
    PartSummary("a", (21, 30), "テスト", start=300.0, end=600.0),
]

INTERVALS = {"keep_intervals": [{"start": 0.0, "end": 600.0}]}


def _write(tmp_path, cfg_dict, *, parts=PARTS, intervals=INTERVALS, **kwargs):
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(summary_to_dict(ProjectSummary("全体の要約", list(parts)))),
        encoding="utf-8",
    )
    out = tmp_path / "publish.json"
    md = tmp_path / "publish.md"
    publish_run.run_publish(
        summary,
        out,
        cfg_dict,
        ordered=[("a", intervals)],
        markdown=md,
        **kwargs,
    )
    return json.loads(out.read_text(encoding="utf-8")), md


def _copy(**kwargs) -> PublishCopy:
    base = {
        "titles": ["候補1", "候補2"],
        "lead": "リード文。",
        "chapter_titles": {1: "おさらい", 2: "取り付け", 3: "テスト"},
        "thumbnail_copy": [
            ThumbSet(lines=[ThumbLine("tag", "水槽DIY"), ThumbLine("hook", "水浸し！")], style={})
        ],
    }
    base.update(kwargs)
    return PublishCopy(**base)


def _fake_generate(monkeypatch, copy: PublishCopy, seen: dict | None = None):
    def fake(project_summary, cfg, **kwargs):
        if seen is not None:
            seen["cfg"] = cfg
            seen["summary"] = project_summary
            seen.update(kwargs)
        return copy

    monkeypatch.setattr(publish_run, "generate_publish_copy", fake)


def test_disabled_writes_an_empty_artifact(tmp_path):
    data, md = _write(tmp_path, {"publish": {"enabled": False}})
    assert data == {
        "titles": [],
        "lead": "",
        "description": "",
        "chapters": [],
        "chapters_qualify": False,
        "chapter_issues": [],
        "thumbnail_copy": [],
        "thumbnails": [],
    }
    assert "disabled" in md.read_text(encoding="utf-8")


def test_enabled_writes_titles_and_description_with_chapters(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy())
    data, md = _write(tmp_path, {"publish": {"enabled": True}})

    assert data["titles"] == ["候補1", "候補2"]
    assert data["chapters"] == [
        {"time": 0.0, "timestamp": "0:00", "title": "おさらい"},
        {"time": 100.0, "timestamp": "1:40", "title": "取り付け"},
        {"time": 300.0, "timestamp": "5:00", "title": "テスト"},
    ]
    assert data["description"] == "リード文。\n\n0:00 おさらい\n1:40 取り付け\n5:00 テスト"
    assert data["chapters_qualify"] is True
    assert data["chapter_issues"] == []
    assert data["thumbnail_copy"] == [
        {
            "lines": [{"role": "tag", "text": "水槽DIY"}, {"role": "hook", "text": "水浸し！"}],
            "background": "",
        }
    ]
    text = md.read_text(encoding="utf-8")
    assert "候補1" in text and "0:00 おさらい" in text


def test_chapter_titles_fall_back_to_the_part_summary(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy(chapter_titles={2: "取り付け"}))
    data, _ = _write(tmp_path, {"publish": {"enabled": True}})
    assert [c["title"] for c in data["chapters"]] == ["前回のおさらい", "取り付け", "テスト"]


def test_a_fully_cut_part_drops_out_of_the_chapters(tmp_path, monkeypatch):
    """Only this stage can know it: the part exists in summary.json, but
    nothing of it survived into the finished timeline."""
    _fake_generate(monkeypatch, _copy())
    intervals = {"keep_intervals": [{"start": 0.0, "end": 100.0}, {"start": 300.0, "end": 600.0}]}
    data, _ = _write(tmp_path, {"publish": {"enabled": True}}, intervals=intervals)
    assert [c["title"] for c in data["chapters"]] == ["おさらい", "テスト"]
    # the third part now starts where the second one's footage would have been
    assert data["chapters"][1]["timestamp"] == "1:40"


def test_a_part_inside_a_timelapse_gets_its_compressed_position(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy())
    intervals = {
        "keep_intervals": [{"start": 0.0, "end": 600.0}],
        "speed_ranges": [{"start": 0.0, "end": 300.0, "factor": 10.0}],
    }
    data, _ = _write(tmp_path, {"publish": {"enabled": True}}, intervals=intervals)
    # 0-100s and 100-300s of source are compressed 10x -> 0s and 10s
    assert [c["time"] for c in data["chapters"]] == [0.0, 10.0, 30.0]


def test_short_chapters_are_merged_and_the_first_is_forced_to_zero(tmp_path, monkeypatch):
    parts = [
        PartSummary("a", (1, 10), "one", start=20.0, end=100.0),
        PartSummary("a", (11, 20), "two", start=100.0, end=104.0),
        PartSummary("a", (21, 30), "three", start=104.0, end=600.0),
    ]
    _fake_generate(monkeypatch, _copy(chapter_titles={}))
    data, _ = _write(tmp_path, {"publish": {"enabled": True}}, parts=parts)
    assert [c["title"] for c in data["chapters"]] == ["one", "three"]
    assert data["chapters"][0]["timestamp"] == "0:00"
    assert data["chapters_qualify"] is False
    assert data["chapter_issues"]


def test_chapters_are_written_even_when_they_cannot_qualify(tmp_path, monkeypatch):
    """YouTube auto-links timestamps regardless, so a list of two still lets a
    viewer jump -- it just does not draw the segmented progress bar."""
    parts = [
        PartSummary("a", (1, 10), "one", start=0.0, end=300.0),
        PartSummary("a", (11, 20), "two", start=300.0, end=600.0),
    ]
    _fake_generate(monkeypatch, _copy(chapter_titles={}))
    data, md = _write(tmp_path, {"publish": {"enabled": True}}, parts=parts)
    assert len(data["chapters"]) == 2
    assert data["chapters_qualify"] is False
    assert "0:00 one" in data["description"]
    assert "0:00 one" in md.read_text(encoding="utf-8")


def test_thumbnail_shortlist_is_recorded_with_finished_positions(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy())
    shots = [
        ThumbShot("a", 150.0, "overlay", "水浸し！", "frames/a/150.000.jpg"),
        ThumbShot("a", 450.0, "keep", "cleanup", "frames/a/450.000.jpg"),
    ]
    data, md = _write(tmp_path, {"publish": {"enabled": True}}, thumbs=shots)
    assert data["thumbnails"] == [
        {
            "stem": "a",
            "source_time": 150.0,
            "timeline_time": 150.0,
            "kind": "overlay",
            "label": "水浸し！",
            "path": "frames/a/150.000.jpg",
        },
        {
            "stem": "a",
            "source_time": 450.0,
            "timeline_time": 450.0,
            "kind": "keep",
            "label": "cleanup",
            "path": "frames/a/450.000.jpg",
        },
    ]
    assert "frames/a/150.000.jpg" in md.read_text(encoding="utf-8")


def test_overlay_texts_reach_the_llm(tmp_path, monkeypatch):
    seen: dict = {}
    _fake_generate(monkeypatch, _copy(), seen)
    _write(
        tmp_path,
        {"publish": {"enabled": True}},
        overlay_texts={"a": ["水浸し！"]},
    )
    assert seen["overlay_texts"] == ["水浸し！"]


def test_plan_directions_reach_the_llm(tmp_path, monkeypatch):
    seen: dict = {}
    _fake_generate(monkeypatch, _copy(), seen)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"directions": [{"stem": "a", "lines": [1, 10], "direction": "feature"}]}),
        encoding="utf-8",
    )
    _write(tmp_path, {"publish": {"enabled": True}}, plan_json=plan)
    assert [d.direction for d in seen["directions"]] == ["feature"]


def test_missing_plan_file_degrades_without_failing(tmp_path, monkeypatch):
    seen: dict = {}
    _fake_generate(monkeypatch, _copy(), seen)
    _write(tmp_path, {"publish": {"enabled": True}}, plan_json=tmp_path / "nope.json")
    assert seen["directions"] == []


def test_project_brief_is_appended_to_the_publish_prompt(tmp_path, monkeypatch):
    seen: dict = {}
    _fake_generate(monkeypatch, _copy(), seen)
    _write(
        tmp_path,
        {
            "publish": {"enabled": True, "prompt": "P"},
            "project": {"audience": "DIY hobbyists"},
        },
    )
    assert seen["cfg"]["prompt"].startswith("P\n\n")
    assert seen["cfg"]["prompt"].endswith("- Audience: DIY hobbyists")

    seen.clear()
    _write(tmp_path, {"publish": {"enabled": True, "prompt": "P"}})
    assert seen["cfg"]["prompt"] == "P"


def test_missing_intervals_file_still_writes_the_copy(tmp_path, monkeypatch):
    """The blender stage may have been skipped or the intervals deleted; the
    titles and thumbnail copy do not depend on the timeline."""
    _fake_generate(monkeypatch, _copy())
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(summary_to_dict(ProjectSummary("全体", list(PARTS)))), encoding="utf-8"
    )
    out = tmp_path / "publish.json"
    # A source whose intervals JSON was unreadable is simply not in the
    # finished video, so it contributes no placements and no chapters.
    publish_run.run_publish(
        summary,
        out,
        {"publish": {"enabled": True}},
        ordered=[],
        markdown=tmp_path / "publish.md",
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["titles"] == ["候補1", "候補2"]
    assert data["chapters"] == []


def _sets():
    return [
        ThumbSet(lines=[ThumbLine("tag", "水槽DIY"), ThumbLine("hook", "水浸し！")], style={}),
        ThumbSet(lines=[ThumbLine("hook", "穴あけ不要。")], style={"gravity": "southwest"}),
    ]


def test_the_copy_sets_are_listed_without_images(tmp_path, monkeypatch):
    """publish.json is the contract; the pictures are the render stage's job."""
    _fake_generate(monkeypatch, _copy(thumbnail_copy=_sets()))
    data, md = _write(tmp_path, {"publish": {"enabled": True}})
    assert "renders" not in data
    text = md.read_text(encoding="utf-8")
    assert text.index("### Set 1") < text.index("水浸し！") < text.index("### Set 2")
    assert "穴あけ不要。" in text
    assert "<img" not in text.split("## Thumbnail frame")[0]


def test_the_candidate_table_shows_the_still_not_its_path(tmp_path, monkeypatch):
    _fake_generate(monkeypatch, _copy())
    shots = [ThumbShot("a", 12.0, "overlay", "水浸し！", "frames/a/12.000.jpg")]
    _, md = _write(tmp_path, {"publish": {"enabled": True}}, thumbs=shots)
    text = md.read_text(encoding="utf-8")
    assert '<img src="frames/a/12.000.jpg" width="240">' in text
    assert "`frames/a/12.000.jpg`" not in text


def _markup_md(tmp_path, monkeypatch, markup):
    """publish.md rendered with a still and the given image_markup."""
    _fake_generate(monkeypatch, _copy(thumbnail_copy=_sets()))
    cfg = {"publish": {"enabled": True}}
    if markup is not None:
        cfg["publish"]["image_markup"] = markup
    shots = [ThumbShot("a", 12.0, "overlay", "水浸し！", "frames/a/12.000.jpg")]
    _, md = _write(tmp_path, cfg, thumbs=shots)
    return md.read_text(encoding="utf-8")


def test_markdown_image_markup_uses_markdown_images(tmp_path, monkeypatch):
    """Renderers that don't allow raw HTML (plain markdown viewers) still show
    the stills when image_markup is markdown; the width hint is HTML-only."""
    text = _markup_md(tmp_path, monkeypatch, "markdown")
    assert "![水浸し！](frames/a/12.000.jpg)" in text
    assert "<img" not in text


def test_html_image_markup_is_the_default(tmp_path, monkeypatch):
    """Unset behaves exactly as before: <img> tags with a width."""
    default = _markup_md(tmp_path, monkeypatch, None)
    explicit = _markup_md(tmp_path, monkeypatch, "html")
    assert default == explicit
    assert '<img src="frames/a/12.000.jpg" width="240">' in default
    assert "![" not in default
