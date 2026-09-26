"""The publish stage's place in the pipeline: last, after blender, with its
frame extraction batched into a single whisperx container."""

from __future__ import annotations

import json
import shlex

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


class _NullRec:
    stage = "publish"

    def clear(self):
        pass

    def rebuild_index(self):
        pass


@pytest.fixture
def ctx(tmp_path):
    sources = [SourceMedia(abs_path=tmp_path / "a.mp4", stem="a", relative="a.mp4")]
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


def _enable(ctx, **overrides):
    ctx.cfg["publish"].update({"enabled": True, **overrides})


def _director(ctx, ops):
    path = ctx.stage_dir("director") / "a_director.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ops": ops}), encoding="utf-8")


def _segments(ctx, count, seconds=10.0):
    path = ctx.stage_dir("sentence_split") / "a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": i * seconds, "end": (i + 1) * seconds, "text": f"line {i + 1}"}
                    for i in range(count)
                ]
            }
        ),
        encoding="utf-8",
    )


def test_publish_runs_after_blender_and_before_render():
    assert st.STAGE_NAMES.index("publish") == st.STAGE_NAMES.index("blender") + 1
    assert st.STAGE_NAMES.index("render") == st.STAGE_NAMES.index("publish") + 1
    assert [s.name for s in st.STAGES] == st.STAGE_NAMES


def test_publish_required_output(ctx):
    stage = next(s for s in st.STAGES if s.name == "publish")
    assert stage.required_outputs(ctx) == [ctx.stage_dir("publish") / "publish.json"]


def _intervals(ctx, stem, data):
    d = ctx.output_dir / "intervals"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}_intervals.json").write_text(json.dumps(data), encoding="utf-8")


def test_adapter_passes_project_paths(ctx, monkeypatch):
    seen = {}
    _intervals(ctx, "a", {"duration_sec": 10.0, "keep_intervals": [{"start": 0.0, "end": 10.0}]})
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st, "run_publish", lambda s, o, cfg, **kw: seen.update(kw, summary=s, out=o)
    )
    next(s for s in st.STAGES if s.name == "publish").run(ctx)
    out = ctx.output_dir
    assert seen["summary"] == out / "summary" / "summary.json"
    assert seen["out"] == out / "publish" / "publish.json"
    assert seen["markdown"] == out / "publish" / "publish.md"
    assert seen["frames_json"] == out / "publish" / "frames.json"
    assert seen["plan"] == ""  # no director/plan.md yet
    # The finished video, already sliced to the manifest's playback order --
    # publish never re-derives a time from a line number.
    assert [stem for stem, _ in seen["ordered"]] == ["a"]


def test_disabled_extracts_no_frames(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(st, "run_command", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(st, "run_publish", lambda *a, **k: None)
    _director(ctx, [{"type": "overlay", "lines": [1, 1], "text": "x", "duration": 2.0}])
    _segments(ctx, 2)
    next(s for s in st.STAGES if s.name == "publish").run(ctx)
    assert calls == []


def test_enabled_extracts_every_candidate_in_one_container(ctx, monkeypatch):
    scripts = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    seen = {}

    def fake_run_command(cmd, **kwargs):
        scripts.append(cmd[-1])
        for line in cmd[-1].splitlines():
            tokens = shlex.split(line)
            path = ctx.output_dir / tokens[-3].removeprefix("/output/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"jpeg")

    monkeypatch.setattr(st, "run_command", fake_run_command)
    monkeypatch.setattr(st, "run_publish", lambda s, o, cfg, **kw: seen.update(kw))
    _enable(ctx)
    _segments(ctx, 4)
    _director(
        ctx,
        [
            {"type": "overlay", "lines": [1, 1], "text": "水浸し！", "duration": 2.0},
            {"type": "timelapse", "lines": [2, 3], "factor": 8.0, "text": "配管作業"},
        ],
    )
    next(s for s in st.STAGES if s.name == "publish").run(ctx)

    assert len(scripts) == 1, "one docker run for the whole stage, not one per frame"
    assert scripts[0].count("ffmpeg") == 3  # overlay midpoint + both timelapse boundaries
    assert [t.path for t in seen["thumbs"]] == [
        "frames/a/5.000.jpg",
        "frames/a/10.000.jpg",
        "frames/a/30.000.jpg",
    ]
    assert seen["overlay_texts"] == {"a": ["水浸し！", "配管作業"]}


def test_frames_that_ffmpeg_never_wrote_are_dropped(ctx, monkeypatch):
    seen = {}
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(st, "run_command", lambda *a, **k: None)  # writes nothing
    monkeypatch.setattr(st, "run_publish", lambda s, o, cfg, **kw: seen.update(kw))
    _enable(ctx)
    _segments(ctx, 2)
    _director(ctx, [{"type": "overlay", "lines": [1, 1], "text": "x", "duration": 2.0}])
    next(s for s in st.STAGES if s.name == "publish").run(ctx)
    assert seen["thumbs"] == []


def test_failed_extraction_does_not_abort_the_stage(ctx, monkeypatch):
    seen = {}
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())

    def boom(*a, **k):
        raise RuntimeError("docker is not running")

    monkeypatch.setattr(st, "run_command", boom)
    monkeypatch.setattr(st, "run_publish", lambda s, o, cfg, **kw: seen.update(kw))
    _enable(ctx)
    _segments(ctx, 2)
    _director(ctx, [{"type": "overlay", "lines": [1, 1], "text": "x", "duration": 2.0}])
    next(s for s in st.STAGES if s.name == "publish").run(ctx)
    assert seen["thumbs"] == []


def test_max_frames_caps_the_extraction(ctx, monkeypatch):
    scripts = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(st, "run_command", lambda cmd, **k: scripts.append(cmd[-1]))
    monkeypatch.setattr(st, "run_publish", lambda *a, **k: None)
    _enable(ctx, max_frames=1)
    _segments(ctx, 4)
    _director(
        ctx,
        [
            {"type": "overlay", "lines": [1, 1], "text": "hook", "duration": 2.0},
            {"type": "keep", "lines": [3, 4], "note": "event"},
        ],
    )
    next(s for s in st.STAGES if s.name == "publish").run(ctx)
    assert scripts[0].count("ffmpeg") == 1
    assert "frames/a/5.000.jpg" in scripts[0]  # the overlay outranks the keep


def test_missing_director_json_leaves_the_shortlist_empty(ctx, monkeypatch):
    seen = {}
    calls = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(st, "run_command", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(st, "run_publish", lambda s, o, cfg, **kw: seen.update(kw))
    _enable(ctx)
    next(s for s in st.STAGES if s.name == "publish").run(ctx)
    assert seen["thumbs"] == []
    assert calls == []


def test_the_publish_stage_composites_nothing(ctx, monkeypatch):
    """No magick from here: publish writes the contract, render reads it."""
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(st, "run_command", lambda *a, **k: None)

    def boom(*a, **k):
        raise AssertionError("publish must not run ImageMagick")

    monkeypatch.setattr(st, "run_magick", boom)
    monkeypatch.setattr(st, "run_publish", lambda *a, **k: None)
    _enable(ctx)
    st._publish_run(ctx)
