"""Publish edit parsing and validation: every problem at once."""

from __future__ import annotations

import copy

from nagare_clip.publish.timeline import Placement
from nagare_clip.publish.tools.edit import (
    ChapterRef,
    ResolvedChapter,
    assemble_description,
    validate_edit,
)

FONTS = {"sans-bold": "Noto Sans CJK JP Bold"}

# line 1 = a 1-2, line 2 = silence (dropped), line 3 = a 5-6
PL = [Placement("a", 1.0, 2.0, 0.0, 12.0, 1.0), Placement("a", 5.0, 6.0, 12.0, 30.0, 1.0)]

GOOD = {
    "titles": ["title one"],
    "lead": "lead",
    "chapters": [{"line": 1, "title": "Open"}, {"line": 3, "title": "Next"}],
    "thumbnails": [
        {
            "background": "frames/a/1.000.jpg",
            "gravity": "south",
            "lines": [{"text": "hello", "font": "sans-bold"}],
        }
    ],
}


def _publish_dir(tmp_path):
    d = tmp_path / "pub"
    (d / "frames" / "a").mkdir(parents=True, exist_ok=True)
    (d / "frames" / "a" / "1.000.jpg").write_bytes(b"x")
    return d


def _run(tmp_path, project, data, placements=PL, min_chapter=5.0):
    return validate_edit(
        data,
        project,
        placements,
        fonts=FONTS,
        publish_dir=_publish_dir(tmp_path),
        min_chapter=min_chapter,
    )


def _edit(**over):
    d = copy.deepcopy(GOOD)
    d.update(over)
    return d


def test_valid_edit_has_no_problems(tmp_path, project):
    edit, chapters, problems, warnings = _run(tmp_path, project, GOOD)
    assert problems == []
    assert edit is not None
    assert edit.titles == ["title one"] and edit.lead == "lead"
    assert edit.chapters == [ChapterRef(1, "Open"), ChapterRef(3, "Next")]
    assert chapters == [ResolvedChapter(1, 0.0, "Open"), ResolvedChapter(3, 12.0, "Next")]
    assert warnings  # only two chapters


def test_all_problems_reported_at_once(tmp_path, project):
    data = _edit(titles=[])
    data["thumbnails"] = [
        {"background": "nope.jpg", "gravity": "sideways", "lines": [{"text": "x"}]},
        {"lines": [{"text": "y"}]},
    ]
    _, _, problems, _ = _run(tmp_path, project, data)
    assert len(problems) >= 4
    data2 = _edit(titles=["x" * 101])
    data2["thumbnails"][0]["gravity"] = "sideways"
    data2["thumbnails"][0]["background"] = "missing.jpg"
    _, _, p2, _ = _run(tmp_path, project, data2)
    assert len(p2) >= 3


def test_title_with_angle_bracket(tmp_path, project):
    for bad in ("a<b", "a>b"):
        _, _, problems, _ = _run(tmp_path, project, _edit(titles=[bad]))
        assert any(p.startswith("titles[0]") for p in problems)


def test_title_length_boundary(tmp_path, project):
    _, _, ok, _ = _run(tmp_path, project, _edit(titles=["x" * 100]))
    assert ok == []
    _, _, bad, _ = _run(tmp_path, project, _edit(titles=["x" * 101]))
    assert any(p.startswith("titles[0]") for p in bad)


def test_description_over_5000(tmp_path, project):
    _, _, problems, _ = _run(tmp_path, project, _edit(lead="x" * 5000))
    assert any("description" in p and "5000" in p for p in problems)
    _, _, ok, _ = _run(tmp_path, project, _edit(lead="x" * 100))
    assert not any("description" in p for p in ok)


def test_assemble_description():
    from nagare_clip.publish.chapters import Chapter

    ch = [Chapter(0.0, "A"), Chapter(65.0, "B")]
    assert assemble_description("lead", ch) == "lead\n\n0:00 A\n1:05 B"
    assert assemble_description("", ch) == "0:00 A\n1:05 B"
    assert assemble_description("lead", []) == "lead"


def test_line_out_of_range_is_a_problem(tmp_path, project):
    data = _edit(chapters=[{"line": 99, "title": "x"}])
    _, _, problems, _ = _run(tmp_path, project, data)
    assert any(p.startswith("chapters[0] (line 99)") for p in problems)
    data = _edit(chapters=[{"line": 0, "title": "x"}])
    _, _, problems, _ = _run(tmp_path, project, data)
    assert any(p.startswith("chapters[0] (line 0)") for p in problems)


def test_chapter_on_dropped_silence_is_a_problem(tmp_path, project):
    data = _edit(chapters=[{"line": 1, "title": "a"}, {"line": 2, "title": "gone"}])
    _, _, problems, _ = _run(tmp_path, project, data)
    assert any(p.startswith("chapters[1] (line 2)") and "cut" in p for p in problems)


def test_two_chapters_same_time(tmp_path, project):
    data = _edit(chapters=[{"line": 1, "title": "a"}, {"line": 1, "title": "b"}])
    _, _, problems, _ = _run(tmp_path, project, data)
    assert any("same time" in p for p in problems)


def test_short_chapter_is_a_problem(tmp_path, project):
    pl = [Placement("a", 1.0, 2.0, 0.0, 9.0, 1.0), Placement("a", 5.0, 6.0, 9.0, 30.0, 1.0)]
    _, _, problems, _ = _run(tmp_path, project, GOOD, placements=pl, min_chapter=10.0)
    assert any("10" in p and "chapters[0]" in p for p in problems)
    _, _, ok, _ = _run(tmp_path, project, GOOD, placements=pl, min_chapter=9.0)
    assert ok == []


def test_chapters_need_timeline(tmp_path, project):
    _, _, problems, _ = _run(tmp_path, project, GOOD, placements=None)
    assert any("timeline" in p for p in problems)
    _, _, ok, _ = _run(tmp_path, project, _edit(chapters=[]), placements=None)
    assert ok == []


def test_fewer_than_three_chapters_warns(tmp_path, project):
    _, _, problems, warnings = _run(tmp_path, project, GOOD)
    assert problems == [] and warnings


def test_three_chapters_no_warning(tmp_path, project):
    pl = [
        Placement("a", 1.0, 2.0, 0.0, 12.0, 1.0),
        Placement("a", 2.0, 5.0, 12.0, 24.0, 1.0),
        Placement("a", 5.0, 6.0, 24.0, 40.0, 1.0),
    ]
    data = _edit(
        chapters=[
            {"line": 1, "title": "a"},
            {"line": 2, "title": "b"},
            {"line": 3, "title": "c"},
        ]
    )
    _, _, problems, warnings = _run(tmp_path, project, data, placements=pl)
    assert problems == [] and warnings == []


def test_chapters_follow_reordered_timeline(tmp_path, project):
    pl = [Placement("a", 5.0, 6.0, 0.0, 12.0, 1.0), Placement("a", 1.0, 2.0, 12.0, 30.0, 1.0)]
    _, chapters, problems, _ = _run(tmp_path, project, GOOD, placements=pl)
    assert problems == []
    assert [c.line for c in chapters] == [3, 1]


def test_background_required_and_must_exist(tmp_path, project):
    data = _edit()
    del data["thumbnails"][0]["background"]
    _, _, problems, _ = _run(tmp_path, project, data)
    assert any(p.startswith("thumbnails[0]") and "background" in p for p in problems)
    data = _edit()
    data["thumbnails"][0]["background"] = "frames/a/2.000.jpg"
    _, _, problems, _ = _run(tmp_path, project, data)
    assert any(p.startswith("thumbnails[0]") and "background" in p for p in problems)


def test_absolute_background_accepted(tmp_path, project):
    img = tmp_path / "my dir" / "pic 1.jpg"
    img.parent.mkdir()
    img.write_bytes(b"x")
    data = _edit()
    data["thumbnails"][0]["background"] = str(img)
    _, _, problems, _ = _run(tmp_path, project, data)
    assert problems == []


def test_unknown_top_level_key(tmp_path, project):
    _, _, problems, _ = _run(tmp_path, project, _edit(extra=1))
    assert any("extra" in p for p in problems)


def test_structure_errors(tmp_path, project):
    edit, _, problems, _ = _run(tmp_path, project, [1])
    assert edit is None and problems
    _, _, problems, _ = _run(tmp_path, project, _edit(titles="x"))
    assert any("titles" in p for p in problems)
    _, _, problems, _ = _run(tmp_path, project, _edit(chapters=[{"line": "1", "title": "x"}]))
    assert any("chapters[0]" in p for p in problems)
    _, _, problems, _ = _run(tmp_path, project, _edit(chapters=[{"line": 1, "title": ""}]))
    assert any("chapters[0]" in p for p in problems)
    _, _, problems, _ = _run(tmp_path, project, _edit(chapters=[{"line": 1, "title": "x", "z": 1}]))
    assert any("chapters[0]" in p and "z" in p for p in problems)


def test_thumbnail_line_problems(tmp_path, project):
    data = _edit()
    data["thumbnails"][0]["lines"] = []
    _, _, p, _ = _run(tmp_path, project, data)
    assert any(x.startswith("thumbnails[0]") and "lines" in x for x in p)
    data["thumbnails"][0]["lines"] = [{"text": "  "}]
    _, _, p, _ = _run(tmp_path, project, data)
    assert any(x.startswith("thumbnails[0].lines[0]") for x in p)
    data["thumbnails"][0]["lines"] = [{"text": "a", "role": "bogus"}]
    _, _, p, _ = _run(tmp_path, project, data)
    assert any(x.startswith("thumbnails[0].lines[0]") and "role" in x for x in p)
    data["thumbnails"][0]["lines"] = [{"text": "a", "font": "nope"}]
    _, _, p, _ = _run(tmp_path, project, data)
    assert any(x.startswith("thumbnails[0].lines[0]") and "font" in x for x in p)


def test_untimed_line_says_no_timing(tmp_path, project):
    object.__setattr__(project.transcripts[0], "seg_times", None)
    _, _, problems, _ = _run(tmp_path, project, GOOD)
    assert any("no timing" in p for p in problems)


def test_resolve_chapters_is_public_and_shared_with_validate_edit(project):
    from nagare_clip.publish.tools.edit import resolve_chapters

    refs = [ChapterRef(3, "Next"), ChapterRef(1, "Open"), ChapterRef(99, "Nope")]
    chapters, problems = resolve_chapters(refs, project, PL, min_chapter=5.0)
    assert chapters == [ResolvedChapter(1, 0.0, "Open"), ResolvedChapter(3, 12.0, "Next")]
    assert problems == ["chapters[2] (line 99): no such line (1-3)"]
    _, _, via_validate, _ = validate_edit(
        {"titles": ["t"], "chapters": [{"line": 99, "title": "Nope"}]},
        project,
        PL,
        fonts=FONTS,
        publish_dir=None,
        min_chapter=5.0,
    )
    assert "chapters[0] (line 99): no such line (1-3)" in via_validate
