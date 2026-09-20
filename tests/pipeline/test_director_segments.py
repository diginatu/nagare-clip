"""The director adapter walks the finished video's SEGMENTS.

The loop is global even when the run is not: --source X narrows what is
processed, not what the finished video contains, so X's segments keep their
position in the whole project's order and see their real neighbours.
"""

from __future__ import annotations

import json

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.director.director_llm import DirectorOp, DirectorResult
from nagare_clip.order import Segment
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


class _NullRec:
    stage = "director"

    def clear(self): ...
    def begin(self, unit): ...
    def attempt(self, **kw): ...
    def flush_unit(self, unit, **kw): ...
    def rebuild_index(self): ...


def _ctx(tmp_path, stems=("mix", "dev")):
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
def project(tmp_path, monkeypatch):
    """'mix' (9 lines) split into three, 'dev' (4 lines) whole.

    Playback order: dev, mix[4-6], mix[1-3], mix[7-9] — so mix[1-3]'s real
    predecessor is another segment of its OWN source.
    """
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    tf = tmp_path / "out" / "text_filter"
    tf.mkdir(parents=True)
    for stem, count in (("mix", 9), ("dev", 4)):
        (in_dir / f"{stem}.mp4").touch()
        (tf / f"{stem}_edits.txt").write_text(
            "\n".join(f"{stem}の{i}行目" for i in range(1, count + 1)) + "\n", encoding="utf-8"
        )
    plan = tmp_path / "out" / "plan"
    plan.mkdir(parents=True)
    plan.joinpath("plan.json").write_text(
        json.dumps(
            {
                "directions": [],
                "order": [
                    {"stem": "dev"},
                    {"stem": "mix", "lines": [4, 6]},
                    {"stem": "mix", "lines": [1, 3]},
                    {"stem": "mix", "lines": [7, 9]},
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _spy(monkeypatch, result=None, fail_on=None):
    calls = []

    def fake(edits_txt, cfg, *, segment, **kw):
        calls.append({"segment": segment, "edits": edits_txt, **kw})
        if fail_on is not None and segment == fail_on:
            return DirectorResult([], ok=False)
        if result is not None:
            return result(segment)
        return DirectorResult([])

    monkeypatch.setattr(st, "run_director", fake)
    return calls


def _run(ctx):
    # The per-segment path, which the stage no longer runs (see
    # stages._director_run): it is driven directly until task 5 deletes it.
    st._director_run_segments(ctx)


class TestTheLoop:
    def test_one_call_per_segment_in_playback_order(self, project, monkeypatch):
        calls = _spy(monkeypatch)
        _run(_ctx(project))
        assert [c["segment"] for c in calls] == [
            Segment("dev", None),
            Segment("mix", (4, 6)),
            Segment("mix", (1, 3)),
            Segment("mix", (7, 9)),
        ]

    def test_every_call_sees_the_whole_order(self, project, monkeypatch):
        calls = _spy(monkeypatch)
        _run(_ctx(project))
        assert all(len(c["all_segments"]) == 4 for c in calls)

    def test_a_source_ops_are_merged_into_one_file_line_sorted(self, project, monkeypatch):
        def result(segment):
            first = (segment.lines or (1, 1))[0]
            return DirectorResult([DirectorOp(type="cut", lines=(first, first))])

        _spy(monkeypatch, result=result)
        _run(_ctx(project))
        data = json.loads(
            (project / "out" / "director" / "mix_director.json").read_text(encoding="utf-8")
        )
        assert [op["lines"] for op in data["ops"]] == [[1, 1], [4, 4], [7, 7]]


class TestSingleSourceRun:
    def test_only_the_selected_sources_segments_are_called(self, project, monkeypatch):
        calls = _spy(monkeypatch)
        _run(_ctx(project, stems=("mix",)))
        assert [c["segment"].stem for c in calls] == ["mix", "mix", "mix"]

    def test_the_position_is_still_the_projects(self, project, monkeypatch):
        # Improvement 19's property, now at segment granularity.
        calls = _spy(monkeypatch)
        _run(_ctx(project, stems=("mix",)))
        assert calls[0]["all_segments"] == [
            Segment("dev", None),
            Segment("mix", (4, 6)),
            Segment("mix", (1, 3)),
            Segment("mix", (7, 9)),
        ]

    def test_the_neighbours_are_the_real_ones(self, project, monkeypatch):
        calls = _spy(monkeypatch)
        _run(_ctx(project, stems=("mix",)))
        assert calls[0]["before"].segment == Segment("dev", None)


class TestSeamsAreSegments:
    def test_the_predecessor_can_be_another_segment_of_the_same_source(self, project, monkeypatch):
        # This is the case a source-granular seam cannot express at all.
        calls = _spy(monkeypatch)
        _run(_ctx(project))
        by_segment = {c["segment"]: c for c in calls}
        entry = by_segment[Segment("mix", (1, 3))]
        assert entry["before"].segment == Segment("mix", (4, 6))
        assert entry["after"].segment == Segment("mix", (7, 9))

    def test_the_first_segment_has_no_predecessor(self, project, monkeypatch):
        calls = _spy(monkeypatch)
        _run(_ctx(project))
        assert calls[0]["before"] is None

    def test_the_last_segment_has_no_successor(self, project, monkeypatch):
        calls = _spy(monkeypatch)
        _run(_ctx(project))
        assert calls[-1]["after"] is None

    def test_a_neighbour_points_at_its_own_sources_edits_file(self, project, monkeypatch):
        calls = _spy(monkeypatch)
        _run(_ctx(project))
        assert calls[1]["before"].edits == project / "out" / "text_filter" / "dev_edits.txt"


class TestPriorCaptions:
    def test_captions_come_from_the_earlier_segments(self, project, monkeypatch):
        def result(segment):
            label = f"{segment.stem}{(segment.lines or ('all',))[0]}"
            return DirectorResult(
                [DirectorOp(type="overlay", lines=(1, 1), text=label, duration=2.0)]
            )

        calls = _spy(monkeypatch, result=result)
        _run(_ctx(project))
        assert calls[0]["prior_captions"] == []
        assert calls[1]["prior_captions"] == ["devall"]
        assert calls[2]["prior_captions"] == ["devall", "mix4"]

    def test_an_earlier_segment_of_a_source_not_in_this_run_is_read_off_disk(
        self, project, monkeypatch
    ):
        d = project / "out" / "director"
        d.mkdir(parents=True, exist_ok=True)
        (d / "dev_director.json").write_text(
            json.dumps(
                {"ops": [{"type": "overlay", "lines": [1, 1], "text": "前回", "duration": 2.0}]}
            ),
            encoding="utf-8",
        )
        calls = _spy(monkeypatch)
        _run(_ctx(project, stems=("mix",)))
        assert calls[0]["prior_captions"] == ["前回"]


class TestFailureIsFatal:
    def test_a_failed_segment_fails_the_run(self, project, monkeypatch):
        _spy(monkeypatch, fail_on=Segment("mix", (1, 3)))
        with pytest.raises(PipelineError):
            _run(_ctx(project))

    def test_a_failed_segment_writes_no_file_for_its_source(self, project, monkeypatch):
        _spy(monkeypatch, fail_on=Segment("mix", (1, 3)))
        with pytest.raises(PipelineError):
            _run(_ctx(project))
        assert not (project / "out" / "director" / "mix_director.json").exists()

    def test_a_stale_file_does_not_survive_a_failed_run(self, project, monkeypatch):
        d = project / "out" / "director"
        d.mkdir(parents=True, exist_ok=True)
        (d / "mix_director.json").write_text(
            json.dumps({"ops": [{"type": "cut", "lines": [1, 1]}]}), encoding="utf-8"
        )
        _spy(monkeypatch, fail_on=Segment("mix", (1, 3)))
        with pytest.raises(PipelineError):
            _run(_ctx(project))
        assert not (d / "mix_director.json").exists()

    def test_a_single_segment_source_is_fatal_too(self, project, monkeypatch):
        # No asymmetry: whether a failure stops the run must not depend on
        # whether the plan happened to split that source.
        _spy(monkeypatch, fail_on=Segment("dev", None))
        with pytest.raises(PipelineError):
            _run(_ctx(project))

    def test_no_ops_is_not_a_failure(self, project, monkeypatch):
        _spy(monkeypatch)
        _run(_ctx(project))
        data = json.loads(
            (project / "out" / "director" / "dev_director.json").read_text(encoding="utf-8")
        )
        assert data == {"ops": []}


def _plan(tmp_path, order, directions=()):
    d = tmp_path / "out" / "plan"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(
        json.dumps({"directions": list(directions), "order": order}), encoding="utf-8"
    )


def _director_file(tmp_path, stem, ops):
    d = tmp_path / "out" / "director"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}_director.json").write_text(json.dumps({"ops": ops}), encoding="utf-8")


class TestPriorCaptionsAreOnlyEarlier:
    def test_a_caption_from_later_in_the_timeline_is_not_shown(self, project, monkeypatch):
        # "already shown earlier" must not reach forwards, even when the later
        # source's file is on disk because this run is not rewriting it.
        _plan(
            project,
            [
                {"stem": "mix", "lines": [4, 6]},
                {"stem": "mix", "lines": [1, 3]},
                {"stem": "mix", "lines": [7, 9]},
                {"stem": "dev"},
            ],
        )
        _director_file(
            project,
            "dev",
            [{"type": "overlay", "lines": [1, 1], "text": "あとの字幕", "duration": 2.0}],
        )
        calls = _spy(monkeypatch)
        _run(_ctx(project, stems=("mix",)))
        assert all("あとの字幕" not in c["prior_captions"] for c in calls)


class TestPriorOpsAreFilteredToTheSegment:
    def test_only_the_earlier_segments_own_captions_are_read_off_disk(self, project, monkeypatch):
        # mix plays in three places; dev sits after only the first of them, so
        # only that stretch's caption has been shown by then.
        _plan(
            project,
            [
                {"stem": "mix", "lines": [4, 6]},
                {"stem": "dev"},
                {"stem": "mix", "lines": [1, 3]},
                {"stem": "mix", "lines": [7, 9]},
            ],
        )
        _director_file(
            project,
            "mix",
            [
                {"type": "overlay", "lines": [1, 1], "text": "まだ先の字幕", "duration": 2.0},
                {"type": "overlay", "lines": [5, 5], "text": "もう出た字幕", "duration": 2.0},
            ],
        )
        calls = _spy(monkeypatch)
        _run(_ctx(project, stems=("dev",)))
        assert calls[0]["prior_captions"] == ["もう出た字幕"]


class TestNotesAfterAFailure:
    def test_no_divergence_note_is_written_when_a_segment_failed(self, project, monkeypatch):
        # A note describing ops that are not there is worse than no note.
        # A "speed up" direction diverges precisely when no timelapse op landed
        # in its range -- which is what a failed run leaves behind.
        _plan(
            project,
            [{"stem": "dev"}, {"stem": "mix", "lines": [1, 9]}],
            directions=[{"stem": "mix", "lines": [1, 9], "direction": "speed up — 長い"}],
        )
        _spy(monkeypatch, fail_on=Segment("mix", None))
        with pytest.raises(PipelineError):
            _run(_ctx(project, stems=("mix",)))
        note = project / "out" / "llm_report" / "notes" / "plan_divergence.md"
        assert not note.exists()
