"""The one page at the top of the output directory.

``output/index.md`` is a directory listing with better manners: it names the
deliverable, links the four files written to be read by a human, and states
each one's mtime.  Everything on it is already on disk -- nothing here is
judgement, so nothing here calls a model.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nagare_clip.index_page import INDEX_NAME, build_index, write_index

CFG = {"general": {"image_markup": "html"}}


def _project(tmp_path, name="water_pump_4"):
    """An output dir shaped like a real one: the stage dirs, nothing in them."""
    output = tmp_path / name / "video-editor-ai"
    for stage in ("blender", "intervals", "llm_report", "plan_dialogue", "publish", "render"):
        (output / stage).mkdir(parents=True)
    return output


def _touch(path: Path, text: str = "x", when: str = "2026-09-05 10:33") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M"))
    os.utime(path, (stamp, stamp))
    return path


def _intervals(output, stem, duration, keeps, speed_ranges=()):
    (output / "intervals" / f"{stem}_intervals.json").write_text(
        json.dumps(
            {
                "source_file": stem,
                "duration_sec": duration,
                "keep_intervals": [{"start": a, "end": b} for a, b in keeps],
                "speed_ranges": [{"start": a, "end": b, "factor": f} for a, b, f in speed_ranges],
            }
        ),
        encoding="utf-8",
    )


def _manifest(output, segments):
    (output / "intervals" / "timeline.json").write_text(
        json.dumps({"segments": [{"stem": s, "start": a, "end": b} for s, a, b in segments]}),
        encoding="utf-8",
    )


def _row(md: str, needle: str) -> str:
    matches = [line for line in md.splitlines() if line.startswith("|") and needle in line]
    assert len(matches) == 1, f"expected one row mentioning {needle!r}, got {matches}"
    return matches[0]


class TestWhatIsOnThePage:
    def test_the_heading_is_the_project(self, tmp_path):
        md = build_index(_project(tmp_path), CFG)
        assert md.startswith("# water_pump_4\n")

    def test_each_human_readable_file_is_one_link_away(self, tmp_path):
        output = _project(tmp_path)
        _touch(output / "publish" / "publish.md")
        _touch(output / "render" / "render.md")
        _touch(output / "plan_dialogue" / "history.md")
        _touch(output / "llm_report" / "index.md")
        md = build_index(output, CFG)
        assert "[publish.md](publish/publish.md)" in md
        assert "[render.md](render/render.md)" in md
        assert "[history.md](plan_dialogue/history.md)" in md
        assert "[llm_report/index.md](llm_report/index.md)" in md

    def test_the_blend_is_named(self, tmp_path):
        output = _project(tmp_path)
        _touch(output / "blender" / "a_edited.blend")
        assert "`blender/a_edited.blend`" in build_index(output, CFG)

    def test_the_blend_is_named_even_before_blender_ran(self, tmp_path):
        """The deliverable is the point of the page; name where it will be."""
        output = _project(tmp_path)
        md = build_index(output, CFG, blend=output / "blender" / "a_edited.blend")
        assert "`blender/a_edited.blend`" in md
        assert _row(md, "a_edited.blend").endswith("| — |")

    def test_this_runs_blend_wins_over_another_on_disk(self, tmp_path):
        """A --source run names its own .blend, not the alphabetically first."""
        output = _project(tmp_path)
        _touch(output / "blender" / "a_edited.blend")
        _touch(output / "blender" / "b_edited.blend")
        md = build_index(output, CFG, blend=output / "blender" / "b_edited.blend")
        assert "`blender/b_edited.blend`" in md
        assert "a_edited.blend" not in md

    def test_the_cut_report_note_is_listed(self, tmp_path):
        output = _project(tmp_path)
        _touch(output / "llm_report" / "notes" / "cut_report.md")
        assert "[cut_report.md](llm_report/notes/cut_report.md)" in build_index(output, CFG)


class TestTimestamps:
    def test_a_present_file_carries_its_mtime(self, tmp_path):
        output = _project(tmp_path)
        _touch(output / "publish" / "publish.md", when="2026-09-05 10:33")
        assert _row(build_index(output, CFG), "publish.md").endswith("| 09-05 10:33 |")

    def test_a_missing_file_is_a_dash(self, tmp_path):
        output = _project(tmp_path)
        assert _row(build_index(output, CFG), "cut_report.md").endswith("| — |")

    def test_a_missing_file_is_named_but_not_linked(self, tmp_path):
        """A link to a file that is not there is worse than a path."""
        md = build_index(_project(tmp_path), CFG)
        assert "`llm_report/notes/cut_report.md`" in md
        assert "](llm_report/notes/cut_report.md)" not in md

    def test_no_row_explains_an_absence(self, tmp_path):
        """A timestamp is a fact; an explanation is a guess by a writer who
        cannot see the run, and a wrong one is worse than a dash."""
        output = _project(tmp_path)
        _touch(output / "publish" / "publish.md")
        md = build_index(output, CFG)
        for row in [line for line in md.splitlines() if line.startswith("| ")]:
            assert row.count("|") == 3, row
        lowered = md.lower()
        for guess in ("has not run", "never ran", "not measured", "because", "missing"):
            assert guess not in lowered

    def test_a_row_whose_file_did_not_change_keeps_its_timestamp(self, tmp_path):
        """Re-running one stage must not disturb the rest of the page."""
        output = _project(tmp_path)
        _touch(output / "publish" / "publish.md", when="2026-08-22 17:59")
        first = build_index(output, CFG)
        _touch(output / "render" / "render.md", when="2026-09-05 10:33")
        second = build_index(output, CFG)
        assert _row(first, "publish.md") == _row(second, "publish.md")
        assert _row(second, "render.md").endswith("| 09-05 10:33 |")


class TestTheHeadline:
    def test_it_measures_the_finished_cut(self, tmp_path):
        output = _project(tmp_path)
        _intervals(output, "one", 100.0, [(0.0, 10.0), (20.0, 100.0)], [(20.0, 100.0, 8.0)])
        _intervals(output, "two", 100.0, [(0.0, 40.0)])
        # 200s of source -> 10 + 10 + 40 = 60s finished
        assert "2 sources · 3.3 min → 1.0 min (30%)" in build_index(output, CFG)

    def test_one_source_is_singular(self, tmp_path):
        output = _project(tmp_path)
        _intervals(output, "one", 120.0, [(0.0, 60.0)])
        assert "1 source · 2.0 min → 1.0 min (50%)" in build_index(output, CFG)

    def test_a_project_where_intervals_never_ran_says_nothing(self, tmp_path):
        """Dashes, and no crash -- not a guess at what the numbers would be."""
        md = build_index(_project(tmp_path), CFG)
        assert "min →" not in md
        assert "sources ·" not in md

    def test_an_unreadable_intervals_file_is_skipped(self, tmp_path):
        output = _project(tmp_path)
        (output / "intervals" / "broken_intervals.json").write_text("{oops", encoding="utf-8")
        _intervals(output, "one", 120.0, [(0.0, 60.0)])
        assert "1 source · 2.0 min → 1.0 min (50%)" in build_index(output, CFG)

    def test_the_manifest_is_the_authority_on_what_is_in_the_cut(self, tmp_path):
        """Only what the finished timeline plays: a stale intervals JSON left
        behind by a source that is no longer in the project is not the cut."""
        output = _project(tmp_path)
        _intervals(output, "one", 100.0, [(0.0, 50.0)])
        _intervals(output, "gone", 900.0, [(0.0, 900.0)])
        _manifest(output, [("one", 0.0, 100.0)])
        assert "1 source · 1.7 min → 0.8 min (50%)" in build_index(output, CFG)

    def test_a_segment_measures_only_its_own_window(self, tmp_path):
        """The manifest is in seconds and it slices; the headline follows it."""
        output = _project(tmp_path)
        _intervals(output, "one", 120.0, [(0.0, 20.0), (60.0, 120.0)])
        _manifest(output, [("one", 0.0, 60.0)])
        assert "1 source · 2.0 min → 0.3 min (17%)" in build_index(output, CFG)


class TestRowsThatCount:
    def test_the_llm_report_row_counts_the_calls(self, tmp_path):
        output = _project(tmp_path)
        _touch(
            output / "llm_report" / "index.md",
            "# LLM Report\n\n248 call(s) — dropped-items: 17, ok: 231\n",
        )
        assert "248 calls, 17 dropped-items" in _row(
            build_index(output, CFG), "llm_report/index.md"
        )

    def test_no_dropped_items_is_not_mentioned(self, tmp_path):
        output = _project(tmp_path)
        _touch(output / "llm_report" / "index.md", "# LLM Report\n\n12 call(s) — ok: 12\n")
        assert "12 calls" in _row(build_index(output, CFG), "llm_report/index.md")
        assert "dropped" not in _row(build_index(output, CFG), "llm_report/index.md")

    def test_an_unparseable_report_falls_back_to_a_plain_description(self, tmp_path):
        output = _project(tmp_path)
        _touch(output / "llm_report" / "index.md", "not a report\n")
        assert "the LLM call table" in _row(build_index(output, CFG), "llm_report/index.md")

    def test_the_publish_row_states_the_title_and_the_chapter_count(self, tmp_path):
        output = _project(tmp_path)
        _touch(output / "publish" / "publish.md")
        (output / "publish" / "publish.json").write_text(
            json.dumps(
                {
                    "titles": ["まさかの水漏れ", "second"],
                    "chapters": [{"time": 0.0}, {"time": 10.0}, {"time": 20.0}],
                }
            ),
            encoding="utf-8",
        )
        row = _row(build_index(output, CFG), "publish.md")
        assert "まさかの水漏れ" in row
        assert "3 chapters" in row

    def test_without_publish_json_the_publish_row_is_plain(self, tmp_path):
        output = _project(tmp_path)
        _touch(output / "publish" / "publish.md")
        assert "titles, chapters, description" in _row(build_index(output, CFG), "publish.md")


class TestThumbnails:
    def _rendered(self, tmp_path):
        output = _project(tmp_path)
        (output / "render" / "thumbnails").mkdir(parents=True)
        (output / "render" / "render.json").write_text(
            json.dumps(
                {
                    "renders": [
                        {"set": 1, "path": "thumbnails/set1.jpg", "background": "frames/a.jpg"},
                        {"set": 2, "path": "thumbnails/set2.jpg", "background": "frames/b.jpg"},
                    ],
                    "skipped": [],
                }
            ),
            encoding="utf-8",
        )
        return output

    def test_html_markup(self, tmp_path):
        md = build_index(self._rendered(tmp_path), {"general": {"image_markup": "html"}})
        assert "## thumbnails" in md
        assert '<img src="render/thumbnails/set1.jpg"' in md
        assert '<img src="render/thumbnails/set2.jpg"' in md

    def test_markdown_markup(self, tmp_path):
        md = build_index(self._rendered(tmp_path), {"general": {"image_markup": "markdown"}})
        assert "![Set 1](render/thumbnails/set1.jpg)" in md
        assert "![Set 2](render/thumbnails/set2.jpg)" in md

    def test_no_renders_means_no_section(self, tmp_path):
        assert "## thumbnails" not in build_index(_project(tmp_path), CFG)


class TestWriting:
    def test_it_lands_at_the_top_of_the_output_dir(self, tmp_path):
        output = _project(tmp_path)
        path = write_index(output, CFG)
        assert path == output / INDEX_NAME
        assert path.read_text(encoding="utf-8").startswith("# water_pump_4")

    def test_rewriting_an_unchanged_project_is_byte_identical(self, tmp_path):
        output = _project(tmp_path)
        _touch(output / "publish" / "publish.md")
        first = write_index(output, CFG).read_text(encoding="utf-8")
        assert write_index(output, CFG).read_text(encoding="utf-8") == first

    def test_the_page_does_not_list_itself(self, tmp_path):
        output = _project(tmp_path)
        write_index(output, CFG)
        assert "](index.md)" not in write_index(output, CFG).read_text(encoding="utf-8")


class TestNoModelIsEverAsked:
    def test_the_module_never_reaches_for_the_llm(self):
        """Zero calls by construction, not by configuration.

        Every provider in this repo goes through ``llm_client.call_llm``, so a
        module that names neither cannot call one -- and a future edit that
        tries to has to trip this first.
        """
        path = Path("src/nagare_clip/index_page.py")
        source = path.read_text(encoding="utf-8")
        offenders = []
        for node in ast.walk(ast.parse(source)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module or ''}.{a.name}" for a in node.names]
            offenders += [f"imports {n}" for n in names if "llm_client" in n]
        if "call_llm" in source:
            offenders.append("mentions call_llm")
        assert offenders == []

    def test_building_the_page_never_loads_the_transport(self, tmp_path):
        """The static guard above, confirmed on the real code path.

        A fresh interpreter, because other tests import the whole pipeline and
        would leave ``llm_client`` in ``sys.modules`` either way.
        """
        output = _project(tmp_path)
        _touch(output / "publish" / "publish.md")
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from nagare_clip.index_page import write_index\n"
            "page = write_index(Path(sys.argv[1]), {'general': {'image_markup': 'html'}})\n"
            "assert 'publish.md' in page.read_text(encoding='utf-8')\n"
            "assert 'nagare_clip.llm_client' not in sys.modules, sorted(sys.modules)\n"
        )
        subprocess.run([sys.executable, "-c", script, str(output)], check=True)


def test_an_output_dir_that_does_not_exist_is_not_a_crash(tmp_path):
    """The page reports what is on disk, including nothing at all."""
    md = build_index(tmp_path / "gone", CFG)
    assert md.startswith("# ")


@pytest.mark.parametrize("markup", ["html", "markdown", "nonsense"])
def test_any_markup_setting_produces_a_page(tmp_path, markup):
    assert build_index(_project(tmp_path), {"general": {"image_markup": markup}})
