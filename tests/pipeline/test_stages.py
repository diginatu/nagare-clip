"""Tests for the real STAGES registry wiring."""

import json
import shlex
import subprocess

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


def _ctx(tmp_path, cfg=None, stems=("a", "b"), from_index=0, to_index=None):
    sources = [
        SourceMedia(abs_path=tmp_path / f"{s}.mp4", stem=s, relative=f"{s}.mp4") for s in stems
    ]
    return PipelineContext(
        cfg=cfg or get_effective_config(None, {}),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=sources,
        from_index=from_index,
        to_index=to_index if to_index is not None else len(st.STAGE_NAMES) - 1,
    )


@pytest.fixture
def gap_ctx(tmp_path):
    sources = [SourceMedia(abs_path=tmp_path / "talk1.mp4", stem="talk1", relative="talk1.mp4")]
    return PipelineContext(
        cfg=get_effective_config(None, {}),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "output",
        sources=sources,
        from_index=0,
        to_index=len(st.STAGE_NAMES) - 1,
    )


def test_stage_names_canonical_order():
    assert st.STAGE_NAMES == [
        "transcription",
        "audio_silence",
        "sentence_split",
        "gap_context",
        "summary",
        "text_filter",
        "plan",
        "director",
        "guided_edit",
        "intervals",
        "blender",
    ]
    assert [s.name for s in st.STAGES] == st.STAGE_NAMES


def test_transcription_required_only_when_sentence_split_runs(tmp_path):
    ctx = _ctx(tmp_path, from_index=st.STAGE_NAMES.index("sentence_split"))
    req = st.STAGES[0].required_outputs(ctx)
    assert (tmp_path / "out" / "transcription" / "a.json") in req
    assert (tmp_path / "out" / "transcription" / "b.txt") in req
    # starting after sentence_split: transcription outputs not needed
    ctx2 = _ctx(tmp_path, from_index=st.STAGE_NAMES.index("text_filter"))
    assert st.STAGES[0].required_outputs(ctx2) == []


def test_per_source_required_outputs(tmp_path):
    ctx = _ctx(tmp_path, from_index=9)
    by_name = {s.name: s for s in st.STAGES}
    assert by_name["audio_silence"].required_outputs(ctx) == [
        tmp_path / "out" / "audio_silence" / "a_cuts.txt",
        tmp_path / "out" / "audio_silence" / "b_cuts.txt",
    ]
    assert by_name["summary"].required_outputs(ctx) == [
        tmp_path / "out" / "summary" / "summary.json"
    ]
    assert by_name["intervals"].required_outputs(ctx) == [
        tmp_path / "out" / "intervals" / "a_intervals.json",
        tmp_path / "out" / "intervals" / "b_intervals.json",
    ]
    assert by_name["blender"].required_outputs(ctx) == []


def test_sentence_split_adapter_clears_recorder_once(tmp_path, monkeypatch):
    calls = []

    class SpyRecorder:
        def clear(self):
            calls.append("clear")

        def rebuild_index(self):
            calls.append("rebuild")

    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: SpyRecorder())
    monkeypatch.setattr(st, "run_sentence_split", lambda *a, **k: calls.append("run"))
    by_name = {s.name: s for s in st.STAGES}
    by_name["sentence_split"].run(_ctx(tmp_path))
    assert calls == ["clear", "run", "run", "rebuild"]


def test_sentence_split_adapter_passes_cuts_path(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_sentence_split",
        lambda ji, ti, oj, ot, cfg, *, stem="", recorder=None, cuts_txt=None: seen.append(
            (ji, oj, cuts_txt)
        ),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["sentence_split"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen == [
        (
            out / "transcription" / "a.json",
            out / "sentence_split" / "a.json",
            out / "audio_silence" / "a_cuts.txt",
        )
    ]


def test_summary_adapter_reads_sentence_split_txts(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_summary",
        lambda txts, out, cfg, *, json_paths=None, gaps_paths=None, cuts_paths=None, recorder=None: (
            seen.update(
                txts=txts,
                out=out,
                json_paths=json_paths,
                gaps_paths=gaps_paths,
                cuts_paths=cuts_paths,
            )
        ),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["summary"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen["txts"] == [out / "sentence_split" / "a.txt"]
    assert seen["json_paths"] == [out / "sentence_split" / "a.json"]
    assert seen["gaps_paths"] == [out / "gap_context" / "a_gaps.json"]
    assert seen["cuts_paths"] == [out / "audio_silence" / "a_cuts.txt"]
    assert seen["out"] == out / "summary" / "summary.json"


def test_director_adapter_passes_context_paths(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_director",
        lambda edits, out, cfg, *, summary=None, plan=None, stem=None, json_path=None, gaps=None, cuts_txt=None, recorder=None: (
            seen.update(
                edits=edits,
                out=out,
                summary=summary,
                plan=plan,
                stem=stem,
                json_path=json_path,
                gaps=gaps,
                cuts_txt=cuts_txt,
            )
        ),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["director"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen["edits"] == out / "text_filter" / "a_edits.txt"
    assert seen["out"] == out / "director" / "a_director.json"
    assert seen["summary"] == out / "summary" / "summary.json"
    assert seen["plan"] == out / "plan" / "plan.json"
    assert seen["stem"] == "a"
    assert seen["json_path"] == out / "sentence_split" / "a.json"
    assert seen["gaps"] == out / "gap_context" / "a_gaps.json"
    assert seen["cuts_txt"] == out / "audio_silence" / "a_cuts.txt"


def test_text_filter_adapter_passes_summary_json(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_text_filter",
        lambda txt, out, cfg, *, summary_json=None, recorder=None: seen.append(
            (txt, out, summary_json)
        ),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["text_filter"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen == [
        (
            out / "sentence_split" / "a.txt",
            out / "text_filter" / "a_edits.txt",
            out / "summary" / "summary.json",
        )
    ]


class _NullRec:
    def clear(self):
        pass

    def rebuild_index(self):
        pass


def test_intervals_adapter_calls_run_per_source(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        st,
        "run_intervals",
        lambda edits, jsonp, out, cfg, *, cuts_txt=None: seen.append((edits, jsonp, out, cuts_txt)),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["intervals"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen == [
        (
            out / "guided_edit" / "a_edits.txt",
            out / "sentence_split" / "a.json",
            out / "intervals" / "a_intervals.json",
            out / "audio_silence" / "a_cuts.txt",
        )
    ]


def test_blender_adapter_builds_command(tmp_path, monkeypatch):
    captured = {}

    def fake_run_command(cmd, **kwargs):
        captured["cmd"] = cmd

    monkeypatch.setattr(st, "run_command", fake_run_command)
    by_name = {s.name: s for s in st.STAGES}
    by_name["blender"].run(_ctx(tmp_path, stems=("a",)))
    cmd = captured["cmd"]
    assert cmd[0] == "blender"
    assert str(tmp_path / "a.mp4") in cmd  # ORIGINAL source path
    assert str(tmp_path / "out" / "blender" / "a_edited.blend") in cmd


def test_transcription_adapter_env_and_cmd(tmp_path, monkeypatch):
    captured = {}

    def fake_run_command(cmd, *, env_extra=None, stderr_to=None):
        captured["cmd"] = cmd
        captured["env"] = env_extra

    monkeypatch.setattr(st, "run_command", fake_run_command)
    st.STAGES[0].run(_ctx(tmp_path, stems=("a",)))
    assert captured["env"] == {
        "INPUT_VIDEOS_DIR": str(tmp_path / "in"),
        "OUTPUT_DIR": str(tmp_path / "out"),
    }
    assert "a.mp4" in captured["cmd"]


def test_gap_context_is_between_sentence_split_and_summary():
    from nagare_clip.pipeline.stages import STAGE_NAMES, STAGES

    assert STAGE_NAMES.index("gap_context") == STAGE_NAMES.index("sentence_split") + 1
    assert STAGE_NAMES.index("gap_context") == STAGE_NAMES.index("summary") - 1
    assert [s.name for s in STAGES] == STAGE_NAMES


def test_gap_context_required_outputs(gap_ctx):
    from nagare_clip.pipeline.stages import STAGES

    stage = next(s for s in STAGES if s.name == "gap_context")
    assert stage.required_outputs(gap_ctx) == [gap_ctx.stage_dir("gap_context") / "talk1_gaps.json"]


def _materialise_frames_from_script(output_dir, script):
    """Write the files ffmpeg would have produced for a batch script.

    Deliberately derives each output path from the SCRIPT TEXT itself
    (parsing each `ffmpeg ... <out_path> || true` line), never from
    `frame_relpath`/`frame_times` -- so this fake can never be
    self-fulfilling. If `_extract_gap_frames` ever mis-assembled a host
    path that doesn't correspond to what it told docker to write, the
    resulting `GapFrames` just wouldn't find a file on disk.
    """
    written = []
    for line in script.splitlines():
        if not line.strip():
            continue
        tokens = shlex.split(line)
        assert tokens[-2:] == ["||", "true"], line
        container_path = tokens[-3]
        rel = container_path.removeprefix("/output/")
        p = output_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"jpeg")
        written.append(container_path)
    return written


def test_gap_context_extracts_all_frames_in_a_single_docker_call(gap_ctx, monkeypatch):
    """Long cut spans -> ONE docker container for the whole stage, not one
    per frame (container startup dominates the real per-frame ffmpeg cost)."""
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("1.000 - 2.000\n10.000 - 20.000\n", encoding="utf-8")

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        _materialise_frames_from_script(gap_ctx.output_dir, cmd[-1])

    captured = {}

    def fake_run_gap_context(gap_frames, output, cfg, **kwargs):
        captured["gap_frames"] = gap_frames
        captured["stem"] = kwargs["stem"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"gaps": []}\n', encoding="utf-8")

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)
    monkeypatch.setattr(stages_mod, "run_gap_context", fake_run_gap_context)
    gap_ctx.cfg["gap_context"]["enabled"] = True

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    stage.run(gap_ctx)

    # Exactly ONE docker invocation for the whole run, carrying all 3 jobs
    # from the one qualifying (10s) gap.
    assert len(commands) == 1
    assert commands[0][-1].count("ffmpeg ") == 3

    gfs = captured["gap_frames"]
    assert len(gfs) == 1
    assert (gfs[0].start, gfs[0].end) == (10.0, 20.0)
    assert len(gfs[0].frames) == 3
    assert gfs[0].relpaths[0] == "frames/talk1/10.200.jpg"
    assert captured["stem"] == "talk1"


def test_gap_context_disabled_runs_no_docker(gap_ctx, monkeypatch):
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("10.000 - 20.000\n", encoding="utf-8")

    run_calls = []

    def record_run_command(*a, **k):
        run_calls.append((a, k))

    def fake_run_gap_context(gap_frames, output, cfg, **kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"gaps": []}\n', encoding="utf-8")

    monkeypatch.setattr(stages_mod, "run_command", record_run_command)
    monkeypatch.setattr(stages_mod, "run_gap_context", fake_run_gap_context)
    gap_ctx.cfg["gap_context"]["enabled"] = False

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    stage.run(gap_ctx)

    # When disabled, no docker calls should be made
    assert run_calls == []
    out = gap_ctx.stage_dir("gap_context") / "talk1_gaps.json"
    assert json.loads(out.read_text(encoding="utf-8")) == {"gaps": []}


def test_gap_context_enabled_but_zero_gaps_runs_no_docker(gap_ctx, monkeypatch):
    """Enabled, but no source has a long-enough gap -> still zero docker
    calls (must never run an empty/no-op container)."""
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("1.000 - 2.000\n", encoding="utf-8")  # below min_gap (3.0s)

    run_calls = []

    def record_run_command(*a, **k):
        run_calls.append((a, k))

    def fake_run_gap_context(gap_frames, output, cfg, **kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"gaps": []}\n', encoding="utf-8")

    monkeypatch.setattr(stages_mod, "run_command", record_run_command)
    monkeypatch.setattr(stages_mod, "run_gap_context", fake_run_gap_context)
    gap_ctx.cfg["gap_context"]["enabled"] = True

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    stage.run(gap_ctx)

    assert run_calls == []


def test_gap_context_batch_call_raising_drops_all_frames_without_aborting(gap_ctx, monkeypatch):
    """If the ONE docker call itself raises (docker missing, image gone),
    every gap simply ends up with no frames -- the pipeline must not abort."""
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("10.000 - 20.000\n", encoding="utf-8")

    def flaky(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    captured = {}

    def fake_run_gap_context(gap_frames, output, cfg, **kwargs):
        captured["gap_frames"] = gap_frames
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"gaps": []}\n', encoding="utf-8")

    monkeypatch.setattr(stages_mod, "run_command", flaky)
    monkeypatch.setattr(stages_mod, "run_gap_context", fake_run_gap_context)
    gap_ctx.cfg["gap_context"]["enabled"] = True

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    stage.run(gap_ctx)  # must not raise

    assert captured["gap_frames"] == []  # gap with zero frames is skipped


def test_gap_context_a_single_bad_line_does_not_drop_other_frames(gap_ctx, monkeypatch):
    """`|| true` on each ffmpeg line means one failed seek inside the batch
    must not take out the other frames/gaps sharing the same container."""
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("10.000 - 20.000\n", encoding="utf-8")

    def fake_run_command(cmd, **kwargs):
        script = cmd[-1]
        lines = [line for line in script.splitlines() if line.strip()]
        # Skip writing the file for the FIRST job only (simulates a failed
        # seek that `|| true` swallowed); the rest still land.
        _materialise_frames_from_script(gap_ctx.output_dir, "\n".join(lines[1:]))

    captured = {}

    def fake_run_gap_context(gap_frames, output, cfg, **kwargs):
        captured["gap_frames"] = gap_frames
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"gaps": []}\n', encoding="utf-8")

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)
    monkeypatch.setattr(stages_mod, "run_gap_context", fake_run_gap_context)
    gap_ctx.cfg["gap_context"]["enabled"] = True

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    stage.run(gap_ctx)

    gfs = captured["gap_frames"]
    assert len(gfs) == 1
    # 3 candidate frame times, 1 missing -> 2 survive; the gap is not dropped.
    assert len(gfs[0].frames) == 2


def test_gap_context_two_sources_share_one_container_without_frame_collision(tmp_path, monkeypatch):
    """Frames for source A and source B, batched in the SAME container call,
    must come back correctly split by stem -- relpaths already namespace by
    stem, but this pins the batching/regrouping logic too."""
    import nagare_clip.pipeline.stages as stages_mod
    from nagare_clip.pipeline.runner import PipelineContext
    from nagare_clip.pipeline.sources import SourceMedia

    sources = [
        SourceMedia(abs_path=tmp_path / "talk1.mp4", stem="talk1", relative="talk1.mp4"),
        SourceMedia(abs_path=tmp_path / "talk2.mp4", stem="talk2", relative="talk2.mp4"),
    ]
    ctx = PipelineContext(
        cfg=get_effective_config(None, {}),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "output",
        sources=sources,
        from_index=0,
        to_index=len(st.STAGE_NAMES) - 1,
    )
    for stem, cuts_text in (
        ("talk1", "10.000 - 20.000\n"),
        ("talk2", "50.000 - 60.000\n"),
    ):
        cuts = ctx.stage_dir("audio_silence") / f"{stem}_cuts.txt"
        cuts.parent.mkdir(parents=True, exist_ok=True)
        cuts.write_text(cuts_text, encoding="utf-8")

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        _materialise_frames_from_script(ctx.output_dir, cmd[-1])

    captured = {}

    def fake_run_gap_context(gap_frames, output, cfg, **kwargs):
        captured[kwargs["stem"]] = gap_frames
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"gaps": []}\n', encoding="utf-8")

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)
    monkeypatch.setattr(stages_mod, "run_gap_context", fake_run_gap_context)
    ctx.cfg["gap_context"]["enabled"] = True

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    stage.run(ctx)

    # ONE container call carries both sources' jobs.
    assert len(commands) == 1
    assert commands[0][-1].count("ffmpeg ") == 6  # 3 frames x 2 sources

    assert set(captured) == {"talk1", "talk2"}
    talk1_gfs, talk2_gfs = captured["talk1"], captured["talk2"]
    assert len(talk1_gfs) == 1 and len(talk2_gfs) == 1
    assert (talk1_gfs[0].start, talk1_gfs[0].end) == (10.0, 20.0)
    assert (talk2_gfs[0].start, talk2_gfs[0].end) == (50.0, 60.0)
    assert all(r.startswith("frames/talk1/") for r in talk1_gfs[0].relpaths)
    assert all(r.startswith("frames/talk2/") for r in talk2_gfs[0].relpaths)
    assert len(talk1_gfs[0].frames) == 3
    assert len(talk2_gfs[0].frames) == 3


def test_extract_gap_frames_takes_all_sources_at_once(gap_ctx, monkeypatch):
    """`_extract_gap_frames` itself: one call across ALL sources' gaps,
    returning a dict keyed by stem."""
    import nagare_clip.pipeline.stages as stages_mod

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        _materialise_frames_from_script(gap_ctx.output_dir, cmd[-1])

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(10.0, 20.0)])])

    assert len(commands) == 1
    assert isinstance(result, dict)
    assert list(result) == ["talk1"]
    assert len(result["talk1"]) == 1
    assert (result["talk1"][0].start, result["talk1"][0].end) == (10.0, 20.0)
    assert len(result["talk1"][0].frames) == 3


def test_extract_gap_frames_no_jobs_makes_zero_docker_calls(gap_ctx, monkeypatch):
    """A source with an empty gap list contributes zero jobs -- no docker
    call should be issued at all."""
    import nagare_clip.pipeline.stages as stages_mod

    run_calls = []
    monkeypatch.setattr(stages_mod, "run_command", lambda *a, **k: run_calls.append((a, k)))

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [])])

    assert run_calls == []
    assert result == {"talk1": []}
