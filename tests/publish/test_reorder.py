"""publish under a reordered timeline.

The chapter timestamps come from build_placements(), which lays segments end to
end in PLAYBACK order.  Computing them in shooting order and then sorting would
be sorting wrong numbers: the cause is the placements, not the ordering of the
entries.
"""

from __future__ import annotations

import json

import nagare_clip.publish.run as publish_run
from nagare_clip.publish.publish_llm import PublishCopy
from nagare_clip.summary.summarize import PartSummary, ProjectSummary, summary_to_dict

# One source, three parts of 100s each, nothing cut.
PARTS = [
    PartSummary("a", (1, 10), "おさらい", start=0.0, end=100.0),
    PartSummary("a", (11, 20), "実演", start=100.0, end=200.0),
    PartSummary("a", (21, 30), "魚", start=200.0, end=300.0),
]
COPY = PublishCopy(
    titles=["題"],
    lead="リード",
    chapter_titles={1: "おさらい", 2: "実演", 3: "魚"},
    thumbnail_copy=[],
)


def _run(tmp_path, ordered, monkeypatch):
    monkeypatch.setattr(publish_run, "generate_publish_copy", lambda project, cfg, **kw: COPY)
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(summary_to_dict(ProjectSummary("全体", list(PARTS)))), encoding="utf-8"
    )
    out = tmp_path / "publish.json"
    publish_run.run_publish(
        summary,
        out,
        {"publish": {"enabled": True, "min_chapter_duration": 10.0}},
        ordered=ordered,
        markdown=tmp_path / "publish.md",
    )
    return json.loads(out.read_text(encoding="utf-8"))


def _seg(start, end):
    return {"keep_intervals": [{"start": start, "end": end}]}


def test_shooting_order_gives_the_parts_in_transcript_order(tmp_path, monkeypatch):
    data = _run(tmp_path, [("a", _seg(0.0, 300.0))], monkeypatch)
    assert [c["title"] for c in data["chapters"]] == ["おさらい", "実演", "魚"]
    assert [c["timestamp"] for c in data["chapters"]] == ["0:00", "1:40", "3:20"]


def test_a_reordered_timeline_reorders_the_chapters(tmp_path, monkeypatch):
    # The middle part plays first: its chapter must come first, and every part
    # must still get one.
    ordered = [
        ("a", _seg(100.0, 200.0)),
        ("a", _seg(0.0, 100.0)),
        ("a", _seg(200.0, 300.0)),
    ]
    data = _run(tmp_path, ordered, monkeypatch)
    assert [c["title"] for c in data["chapters"]] == ["実演", "おさらい", "魚"]
    assert [c["timestamp"] for c in data["chapters"]] == ["0:00", "1:40", "3:20"]


def test_no_chapter_is_lost_to_the_reorder(tmp_path, monkeypatch):
    # build_chapters drops an entry that does not advance, so unsorted entries
    # from a reordered timeline would silently delete chapters.
    ordered = [("a", _seg(200.0, 300.0)), ("a", _seg(0.0, 200.0))]
    data = _run(tmp_path, ordered, monkeypatch)
    assert len(data["chapters"]) == 3
    times = [c["time"] for c in data["chapters"]]
    assert times == sorted(times)


def test_the_captions_are_listed_in_finished_video_order(tmp_path, monkeypatch):
    # publish is told "the edit's own account of where the payoffs are"; in a
    # reordered video that account is in playback order, not shooting order.
    seen = {}

    def fake(project, cfg, **kw):
        seen.update(kw)
        return COPY

    monkeypatch.setattr(publish_run, "generate_publish_copy", fake)
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(summary_to_dict(ProjectSummary("全体", list(PARTS)))), encoding="utf-8"
    )
    publish_run.run_publish(
        summary,
        tmp_path / "publish.json",
        {"publish": {"enabled": True}},
        ordered=[("b", _seg(0.0, 10.0)), ("a", _seg(0.0, 10.0))],
        overlay_texts={"a": ["Aの字幕"], "b": ["Bの字幕"]},
        markdown=tmp_path / "publish.md",
    )
    assert seen["overlay_texts"] == ["Bの字幕", "Aの字幕"]
