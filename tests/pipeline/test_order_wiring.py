"""Tests for resolving the segment order in the orchestrator.

The director is the authority on the order up to and including `intervals`;
this is where its `director/order.json` becomes the list of segments every
stage consults, and where an invalid one falls back to shooting order.
"""

from __future__ import annotations

import json

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.order import Segment
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


def _ctx(tmp_path, stems=("a", "b")):
    sources = [
        SourceMedia(abs_path=tmp_path / f"{s}.mp4", stem=s, relative=f"{s}.mp4") for s in stems
    ]
    return PipelineContext(
        cfg=get_effective_config(None, {}),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=sources,
        from_index=0,
        to_index=len(st.STAGE_NAMES) - 1,
    )


@pytest.fixture
def project(tmp_path):
    """Two sources on disk: 'a' with 5 lines, 'b' with 3."""
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    tf = tmp_path / "out" / "text_filter"
    tf.mkdir(parents=True)
    for stem, count in (("a", 5), ("b", 3)):
        (in_dir / f"{stem}.mp4").touch()
        (tf / f"{stem}_edits.txt").write_text("\n".join(f"line {i}" for i in range(count)) + "\n")
    return tmp_path


def _write_plan(tmp_path, order):
    """Write ``director/order.json`` (the name is the tests' history)."""
    d = tmp_path / "out" / "director"
    d.mkdir(parents=True, exist_ok=True)
    payload = {} if order is None else {"order": order}
    (d / "order.json").write_text(json.dumps(payload), encoding="utf-8")


class TestLineCounts:
    def test_counts_the_lines_of_every_project_source(self, project):
        assert st._line_counts(_ctx(project)) == {"a": 5, "b": 3}

    def test_a_source_with_no_edits_file_is_absent(self, project):
        (project / "out" / "text_filter" / "b_edits.txt").unlink()
        assert st._line_counts(_ctx(project)) == {"a": 5}

    def test_counts_the_project_not_the_run(self, project):
        # --source a still validates the order a full run would.
        assert st._line_counts(_ctx(project, stems=("a",))) == {"a": 5, "b": 3}


class TestTimelineSegments:
    def test_no_order_json_is_shooting_order(self, project):
        assert st._timeline_segments(_ctx(project)) == [Segment("a", None), Segment("b", None)]

    def test_an_order_json_without_an_order_is_shooting_order(self, project):
        _write_plan(project, None)
        assert st._timeline_segments(_ctx(project)) == [Segment("a", None), Segment("b", None)]

    def test_an_order_json_without_an_order_says_nothing_about_validity(self, project, caplog):
        # The default path: "no order" is not a rejected order, and warning
        # about it on every run would drown the warning that matters.
        _write_plan(project, None)
        with caplog.at_level("WARNING"):
            st._timeline_segments(_ctx(project))
        assert caplog.text == ""

    def test_the_order_is_the_projects_even_for_a_single_source_run(self, project):
        # Improvement 19's property, at segment granularity: --source narrows
        # what is processed, not what the finished video contains.
        _write_plan(
            project, [{"stem": "b"}, {"stem": "a", "lines": [3, 5]}, {"stem": "a", "lines": [1, 2]}]
        )
        assert st._timeline_segments(_ctx(project, stems=("a",))) == [
            Segment("b", None),
            Segment("a", (3, 5)),
            Segment("a", (1, 2)),
        ]

    def test_shooting_order_is_the_projects_even_for_a_single_source_run(self, project):
        assert st._timeline_segments(_ctx(project, stems=("a",))) == [
            Segment("a", None),
            Segment("b", None),
        ]

    def test_a_valid_order_is_used(self, project):
        _write_plan(
            project,
            [
                {"stem": "a", "lines": [3, 5]},
                {"stem": "b"},
                {"stem": "a", "lines": [1, 2]},
            ],
        )
        assert st._timeline_segments(_ctx(project)) == [
            Segment("a", (3, 5)),
            Segment("b", None),
            Segment("a", (1, 2)),
        ]

    def test_a_full_range_is_normalised_to_the_whole_source(self, project):
        # Present-and-identity must be indistinguishable from absent.
        _write_plan(project, [{"stem": "a", "lines": [1, 5]}, {"stem": "b", "lines": [1, 3]}])
        assert st._timeline_segments(_ctx(project)) == [Segment("a", None), Segment("b", None)]

    def test_an_order_missing_a_line_falls_back_to_shooting_order(self, project, caplog):
        _write_plan(project, [{"stem": "a", "lines": [1, 4]}, {"stem": "b"}])
        with caplog.at_level("WARNING"):
            assert st._timeline_segments(_ctx(project)) == [Segment("a", None), Segment("b", None)]
        assert "5" in caplog.text

    def test_an_order_missing_a_source_falls_back_to_shooting_order(self, project):
        _write_plan(project, [{"stem": "a"}])
        assert st._timeline_segments(_ctx(project)) == [Segment("a", None), Segment("b", None)]

    def test_an_order_is_rejected_when_the_line_counts_are_unknown(self, project):
        for stem in ("a", "b"):
            (project / "out" / "text_filter" / f"{stem}_edits.txt").unlink()
        _write_plan(project, [{"stem": "b"}, {"stem": "a"}])
        assert st._timeline_segments(_ctx(project)) == [Segment("a", None), Segment("b", None)]

    def test_unreadable_order_json_is_shooting_order(self, project):
        d = project / "out" / "director"
        d.mkdir(parents=True, exist_ok=True)
        (d / "order.json").write_text("{not json", encoding="utf-8")
        assert st._timeline_segments(_ctx(project)) == [Segment("a", None), Segment("b", None)]


def _write_intervals(tmp_path, stem, duration, keeps):
    d = tmp_path / "out" / "intervals"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}_intervals.json").write_text(
        json.dumps(
            {
                "source_file": f"{stem}.mp4",
                "duration_sec": duration,
                "keep_intervals": [{"start": s, "end": e} for s, e in keeps],
                "captions": [],
            }
        ),
        encoding="utf-8",
    )


def _write_sentence_split(tmp_path, stem, times):
    d = tmp_path / "out" / "sentence_split"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.json").write_text(
        json.dumps({"segments": [{"start": s, "end": e, "text": "x"} for s, e in times]}),
        encoding="utf-8",
    )


@pytest.fixture
def built(project):
    """The project, with intervals and sentence_split output on disk."""
    _write_intervals(project, "a", 50.0, [(0.0, 20.0), (30.0, 50.0)])
    _write_intervals(project, "b", 30.0, [(0.0, 30.0)])
    _write_sentence_split(
        project, "a", [(0.0, 5.0), (6.0, 10.0), (12.0, 20.0), (30.0, 40.0), (41.0, 48.0)]
    )
    _write_sentence_split(project, "b", [(0.0, 10.0), (11.0, 20.0), (21.0, 28.0)])
    return project


class TestWriteManifest:
    def test_shooting_order_writes_one_whole_source_entry_each(self, built):
        entries = st._write_manifest(_ctx(built))
        assert [(e.stem, e.start, e.end, e.lines) for e in entries] == [
            ("a", 0.0, 50.0, None),
            ("b", 0.0, 30.0, None),
        ]

    def test_it_lands_in_the_intervals_directory(self, built):
        st._write_manifest(_ctx(built))
        path = built / "out" / "intervals" / "timeline.json"
        assert json.loads(path.read_text())["segments"][0]["stem"] == "a"

    def test_a_reorder_is_resolved_to_seconds_in_playback_order(self, built):
        # Hand-written order, no LLM anywhere: a's tail plays first.
        _write_plan(
            built, [{"stem": "a", "lines": [4, 5]}, {"stem": "b"}, {"stem": "a", "lines": [1, 3]}]
        )
        entries = st._write_manifest(_ctx(built))
        assert [(e.stem, e.start, e.end) for e in entries] == [
            ("a", 20.0, 50.0),
            ("b", 0.0, 30.0),
            ("a", 0.0, 20.0),
        ]

    def test_an_unresolvable_order_falls_back_to_shooting_order(self, built):
        (built / "out" / "sentence_split" / "a.json").unlink()
        _write_plan(
            built, [{"stem": "a", "lines": [4, 5]}, {"stem": "b"}, {"stem": "a", "lines": [1, 3]}]
        )
        entries = st._write_manifest(_ctx(built))
        assert [(e.stem, e.lines) for e in entries] == [("a", None), ("b", None)]


class TestOrderedSources:
    def test_the_finished_video_is_the_manifest_sliced(self, built):
        _write_plan(
            built, [{"stem": "a", "lines": [4, 5]}, {"stem": "b"}, {"stem": "a", "lines": [1, 3]}]
        )
        st._write_manifest(_ctx(built))
        out = st._ordered_sources(_ctx(built))
        assert [stem for stem, _ in out] == ["a", "b", "a"]
        assert out[0][1]["keep_intervals"] == [{"start": 30.0, "end": 50.0}]
        assert out[2][1]["keep_intervals"] == [{"start": 0.0, "end": 20.0}]

    def test_it_is_filtered_to_the_sources_this_run_processes(self, built):
        st._write_manifest(_ctx(built))
        out = st._ordered_sources(_ctx(built, stems=("b",)))
        assert [stem for stem, _ in out] == ["b"]

    def test_no_manifest_degrades_to_shooting_order(self, built):
        out = st._ordered_sources(_ctx(built))
        assert [stem for stem, _ in out] == ["a", "b"]
        assert out[0][1]["keep_intervals"] == [
            {"start": 0.0, "end": 20.0},
            {"start": 30.0, "end": 50.0},
        ]


class TestIntervalsAdapter:
    def test_the_stage_writes_the_manifest(self, built, monkeypatch):
        # intervals is the single conversion point, so the manifest is its
        # output, not something a later stage derives on the fly.
        monkeypatch.setattr(st, "run_intervals", lambda *a, **k: None)
        monkeypatch.setattr(st, "write_cut_report", lambda ctx: None)
        by_name = {s.name: s for s in st.STAGES}
        by_name["intervals"].run(_ctx(built))
        path = built / "out" / "intervals" / "timeline.json"
        assert [e["stem"] for e in json.loads(path.read_text())["segments"]] == ["a", "b"]
