"""Tests for the real STAGES registry wiring."""

import json
import shlex
import subprocess

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.director.director_llm import DirectorResult, ops_from_dict
from nagare_clip.director.run import ConversationResult
from nagare_clip.order import Segment
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
        "plan_revise",
        "director",
        "guided_edit",
        "intervals",
        "blender",
        "publish",
        "render",
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
        lambda edits, cfg, **kw: (seen.update(edits=edits, **kw), DirectorResult([]))[1],
    )
    # The per-segment path the stage no longer runs; driven directly here
    # until task 5 deletes it with the rest of it.
    st._director_run_segments(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen["edits"] == out / "text_filter" / "a_edits.txt"
    assert seen["summary"] == out / "summary" / "summary.json"
    assert seen["plan"] == out / "plan" / "plan.json"
    assert seen["segment"] == Segment("a", None)
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
        lambda edits, jsonp, out, cfg, *, cuts_txt=None, extra=None: seen.append(
            (edits, jsonp, out, cuts_txt)
        ),
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
        if "-filter_complex" in line:
            # SSIM comparison line (Task 9), not a frame-extraction job --
            # this fake only materialises extracted frames.
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

    # Exactly ONE docker invocation for the whole run, carrying all 3
    # extraction jobs from the one qualifying (10s) gap, plus 2 consecutive-
    # pair SSIM comparison jobs (3 frames -> 2 pairs; three-frame comparison
    # follow-up to Task 9's static prefilter).
    assert len(commands) == 1
    assert commands[0][-1].count("ffmpeg ") == 5

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
    # 3 frames x 2 sources (extraction) + 2 consecutive-pair SSIM
    # comparisons per source.
    assert commands[0][-1].count("ffmpeg ") == 10

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


def test_extract_gap_frames_static_ssim_zero_disables_ssim_planning(gap_ctx, monkeypatch):
    """`static_ssim: 0` must disable the SSIM prefilter ENTIRELY -- no
    ssim_jobs planned, no extra ffmpeg line in the batch script, no stats
    file ever requested. Finding 1: previously the threshold was only
    checked in gap_context/run.py at consumption time, so stages.py kept
    planning + running the comparison (and writing stats files) even at
    static_ssim: 0."""
    import nagare_clip.pipeline.stages as stages_mod

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        _materialise_frames_from_script(gap_ctx.output_dir, cmd[-1])

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)
    gap_ctx.cfg["gap_context"]["static_ssim"] = 0

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(10.0, 20.0)])])

    script = commands[0][-1]
    assert "ssim=stats_file=" not in script
    assert "-filter_complex" not in script
    # 3 frame-extraction lines only, no appended SSIM comparison line.
    assert script.count("ffmpeg ") == 3
    assert result["talk1"][0].ssim is None


def _stats_line(score: float) -> str:
    return f"n:1 Y:0.994828 U:0.998750 V:0.998691 All:{score:.6f} (24.123456)\n"


def _write_ssim_stats_for_pairs(gap_ctx, script: str, scores: list[float | None]) -> list[str]:
    """Materialise stats files for each `-filter_complex` (SSIM) line in
    *script*, in order, using *scores* (``None`` skips writing that file --
    simulates a failed/missing comparison). Returns the ssim lines in order."""
    ssim_lines = [line for line in script.splitlines() if "-filter_complex" in line]
    assert len(ssim_lines) == len(scores), (len(ssim_lines), len(scores))
    for line, score in zip(ssim_lines, scores, strict=True):
        if score is None:
            continue
        tokens = shlex.split(line)
        stats_arg = next(t for t in tokens if t.startswith("ssim=stats_file="))
        stats_container_path = stats_arg.removeprefix("ssim=stats_file=")
        rel = stats_container_path.removeprefix("/output/gap_context/")
        stats_host = gap_ctx.stage_dir("gap_context") / rel
        stats_host.parent.mkdir(parents=True, exist_ok=True)
        stats_host.write_text(_stats_line(score), encoding="utf-8")
    return ssim_lines


def test_extract_gap_frames_reads_back_ssim_stats(gap_ctx, monkeypatch):
    """Finding 3: genuine integration coverage of the SSIM planning/readback
    path -- fake stats files with realistic ffmpeg ssim-filter output content
    are materialised at the EXACT container paths the batch script
    requested (derived from the script text, mirroring
    `_materialise_frames_from_script`'s no-self-fulfilling-prophecy
    discipline), then `_extract_gap_frames` must parse them into
    `GapFrames.ssim`.

    This also pins the CONSECUTIVE-pair comparison (the three-frame
    follow-up fix): the gap (10.0-20.0) yields frame times
    [10.200, 15.000, 19.800], so the two ssim jobs must be
    (first, mid) and (mid, last) -- never (first, last) directly, which
    is the bug this change fixes."""
    import nagare_clip.pipeline.stages as stages_mod

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        script = cmd[-1]
        _materialise_frames_from_script(gap_ctx.output_dir, script)
        _write_ssim_stats_for_pairs(gap_ctx, script, [0.998, 0.996132])

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(10.0, 20.0)])])

    script = commands[0][-1]
    ssim_lines = [line for line in script.splitlines() if "-filter_complex" in line]
    assert len(ssim_lines) == 2
    assert "frames/talk1/10.200.jpg" in ssim_lines[0]  # first
    assert "frames/talk1/15.000.jpg" in ssim_lines[0]  # mid
    assert "frames/talk1/10.200.jpg" not in ssim_lines[1]
    assert "frames/talk1/15.000.jpg" in ssim_lines[1]  # mid
    assert "frames/talk1/19.800.jpg" in ssim_lines[1]  # last
    # Never a direct first-vs-last comparison anywhere in the script.
    assert not any(
        "10.200.jpg" in line and "19.800.jpg" in line and "-filter_complex" in line
        for line in script.splitlines()
    )

    assert result["talk1"][0].ssim == pytest.approx(0.996132)  # min(0.998, 0.996132)


def test_extract_gap_frames_ssim_pan_away_and_return_scores_low(gap_ctx, monkeypatch):
    """The bug being fixed: a camera that pans away and returns to the same
    framing scores high on first-vs-last but should NOT be judged static,
    because the middle frame (already extracted, already paid for) shows
    real on-screen action. With min-of-consecutive-pairs, a low first-vs-mid
    score drags the whole gap's ssim below any static threshold even though
    mid-vs-last looks nearly identical (camera settled back)."""
    import nagare_clip.pipeline.stages as stages_mod

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        script = cmd[-1]
        _materialise_frames_from_script(gap_ctx.output_dir, script)
        # first-vs-mid: camera mid-pan, very different (low). mid-vs-last:
        # camera has returned, nearly identical (high). A first-vs-last-only
        # comparison would likely have scored high here (both ends similar).
        _write_ssim_stats_for_pairs(gap_ctx, script, [0.40, 0.985])

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(10.0, 20.0)])])

    assert result["talk1"][0].ssim == pytest.approx(0.40)
    # Well below any sane static threshold -- gap must NOT be prefiltered.
    assert result["talk1"][0].ssim < 0.95


def test_extract_gap_frames_two_frame_gap_ssim_matches_single_pair(gap_ctx, monkeypatch):
    """A gap that collapses to exactly 2 extracted frames has only one
    consecutive pair, so behaves identically to the old first-vs-last
    comparison (there IS no middle frame to ignore)."""
    import nagare_clip.pipeline.stages as stages_mod
    from nagare_clip.gap_context.snapshot import frame_times

    # This span yields exactly 2 frame times (verified below), not 3.
    start, end = 10.0, 10.401
    assert len(frame_times(start, end)) == 2

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        script = cmd[-1]
        _materialise_frames_from_script(gap_ctx.output_dir, script)
        _write_ssim_stats_for_pairs(gap_ctx, script, [0.87])

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(start, end)])])

    assert result["talk1"][0].ssim == pytest.approx(0.87)


def test_extract_gap_frames_one_frame_gap_ssim_stays_none(gap_ctx, monkeypatch):
    """A gap so short it collapses to a single (midpoint-only) frame has no
    pair to compare -- no ssim job planned, `GapFrames.ssim` stays `None`,
    same as today."""
    import nagare_clip.pipeline.stages as stages_mod
    from nagare_clip.gap_context.snapshot import frame_times

    start, end = 10.0, 10.4
    assert len(frame_times(start, end)) == 1

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        script = cmd[-1]
        _materialise_frames_from_script(gap_ctx.output_dir, script)

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(start, end)])])

    script = commands[0][-1]
    assert "-filter_complex" not in script
    assert result["talk1"][0].ssim is None


def test_extract_gap_frames_partial_stats_missing_degrades_to_min_of_survivors(
    gap_ctx, monkeypatch
):
    """One of the two consecutive-pair stats files fails to materialise
    (simulating a failed ffmpeg comparison, `|| true` swallowed it) -- the
    surviving pair's score is still used (min of what parsed), never an
    exception, never silently dropping to None while a real score exists."""
    import nagare_clip.pipeline.stages as stages_mod

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        script = cmd[-1]
        _materialise_frames_from_script(gap_ctx.output_dir, script)
        # Only the SECOND pair's stats file gets written; the first is
        # missing entirely (score=None means "don't write it").
        _write_ssim_stats_for_pairs(gap_ctx, script, [None, 0.72])

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(10.0, 20.0)])])

    assert result["talk1"][0].ssim == pytest.approx(0.72)


def test_extract_gap_frames_unparseable_stats_content_excluded_from_min(gap_ctx, monkeypatch):
    """A stats file that DOES exist but whose content ffmpeg didn't finish
    writing cleanly (unparseable, `parse_ssim_stats` -> None) must be
    excluded from the min just like a missing file -- not treated as a
    0.0/None score that would wrongly win the min() or blow up the
    computation."""
    import nagare_clip.pipeline.stages as stages_mod

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        script = cmd[-1]
        _materialise_frames_from_script(gap_ctx.output_dir, script)
        ssim_lines = [line for line in script.splitlines() if "-filter_complex" in line]
        assert len(ssim_lines) == 2
        for line, content in zip(
            ssim_lines, ["garbage, no All: score here\n", _stats_line(0.81)], strict=True
        ):
            tokens = shlex.split(line)
            stats_arg = next(t for t in tokens if t.startswith("ssim=stats_file="))
            rel = stats_arg.removeprefix("ssim=stats_file=").removeprefix("/output/gap_context/")
            stats_host = gap_ctx.stage_dir("gap_context") / rel
            stats_host.parent.mkdir(parents=True, exist_ok=True)
            stats_host.write_text(content, encoding="utf-8")

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(10.0, 20.0)])])

    assert result["talk1"][0].ssim == pytest.approx(0.81)


def test_director_adapter_passes_the_timeline_position(tmp_path, monkeypatch):
    """Segment n is told the whole order of the finished video.

    With no plan order every source is one whole-source segment, so this is
    shooting order expressed in the new form."""
    seen = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_director",
        lambda edits, cfg, **kw: (
            seen.append((kw["segment"], kw["all_segments"])),
            DirectorResult([]),
        )[1],
    )
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    for s in ("a", "b", "c"):
        (in_dir / f"{s}.mp4").touch()

    # The per-segment path the stage no longer runs; driven directly here
    # until task 5 deletes it with the rest of it.
    st._director_run_segments(_ctx(tmp_path, stems=("a", "b", "c")))

    order = [Segment("a", None), Segment("b", None), Segment("c", None)]
    assert [s[0] for s in seen] == order
    assert all(s[1] == order for s in seen)


def test_director_adapter_passes_the_neighbouring_transcripts(tmp_path, monkeypatch):
    """Each segment is handed the text_filter transcript of the segments playing
    either side of it; the first has no predecessor and the last no successor."""
    seen = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_director",
        lambda edits, cfg, **kw: (
            seen.append(
                (
                    kw["segment"].stem,
                    kw["before"].edits if kw["before"] else None,
                    kw["after"].edits if kw["after"] else None,
                )
            ),
            DirectorResult([]),
        )[1],
    )
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    for s in ("a", "b", "c"):
        (in_dir / f"{s}.mp4").touch()

    # The per-segment path the stage no longer runs; driven directly here
    # until task 5 deletes it with the rest of it.
    st._director_run_segments(_ctx(tmp_path, stems=("a", "b", "c")))

    tf = tmp_path / "out" / "text_filter"
    assert seen == [
        ("a", None, tf / "b_edits.txt"),
        ("b", tf / "a_edits.txt", tf / "c_edits.txt"),
        ("c", tf / "b_edits.txt", None),
    ]


def test_director_adapter_recovers_the_order_for_a_single_source_run(tmp_path, monkeypatch):
    """`--source b.mp4` narrows what is processed, not what the finished video
    contains: the position and the neighbours still come from the input
    directory, so a re-run of one source sees what a full run saw."""
    seen = {}
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st, "run_director", lambda edits, cfg, **kw: (seen.update(kw), DirectorResult([]))[1]
    )
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    for s in ("a", "b", "c"):
        (in_dir / f"{s}.mp4").touch()

    # The per-segment path the stage no longer runs; driven directly here
    # until task 5 deletes it with the rest of it.
    st._director_run_segments(_ctx(tmp_path, stems=("b",)))

    assert seen["all_segments"] == [
        Segment("a", None),
        Segment("b", None),
        Segment("c", None),
    ]
    tf = tmp_path / "out" / "text_filter"
    assert seen["before"].segment == Segment("a", None)
    assert seen["before"].edits == tf / "a_edits.txt"
    assert seen["after"].segment == Segment("c", None)
    assert seen["after"].edits == tf / "c_edits.txt"


def test_director_adapter_falls_back_to_the_processed_stems(tmp_path, monkeypatch):
    """No readable input dir (sources given by absolute path elsewhere): degrade
    to the sources this run knows about rather than failing."""
    seen = {}
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st, "run_director", lambda edits, cfg, **kw: (seen.update(kw), DirectorResult([]))[1]
    )
    # The per-segment path the stage no longer runs; driven directly here
    # until task 5 deletes it with the rest of it.
    st._director_run_segments(_ctx(tmp_path, stems=("a",)))
    assert seen["all_segments"] == [Segment("a", None)]


def test_plan_adapter_passes_history_path(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_plan",
        lambda summary, out, cfg, **kw: seen.update(summary=summary, out=out, **kw),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["plan"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen["summary"] == out / "summary" / "summary.json"
    assert seen["out"] == out / "plan" / "plan.json"
    assert seen["history"] == out / "plan_dialogue" / "history.md"


def _director_returning(ops):
    """A fake director conversation that emits *ops* for source ``a``.

    The divergence note is built from what this run wrote, and the adapter
    deletes a stale file before the conversation (so a failure cannot leave one
    standing), so the ops have to come from the call, not from disk.
    """

    def fake(inputs, cfg, **kw):
        return ConversationResult(ops={"a": ops_from_dict({"ops": ops}, None)})

    return fake


def _plan_and_ops(tmp_path, direction, ops):
    out = tmp_path / "out"
    (out / "plan").mkdir(parents=True, exist_ok=True)
    (out / "director").mkdir(parents=True, exist_ok=True)
    (out / "plan" / "plan.json").write_text(
        json.dumps({"directions": [{"stem": "a", "lines": [1, 10], "direction": direction}]}),
        encoding="utf-8",
    )
    (out / "director" / "a_director.json").write_text(json.dumps({"ops": ops}), encoding="utf-8")
    return out / "llm_report" / "notes" / "plan_divergence.md"


def test_director_adapter_writes_plan_divergence_note(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    ops = [{"type": "cut", "lines": [1, 9], "note": "a long digression"}]
    monkeypatch.setattr(st, "run_director_conversation", _director_returning(ops))
    note = _plan_and_ops(tmp_path, "feature — the payoff", ops)
    by_name = {s.name: s for s in st.STAGES}
    by_name["director"].run(_ctx(tmp_path, stems=("a",)))
    text = note.read_text(encoding="utf-8")
    assert "a [1-10]" in text
    assert "a long digression" in text


def test_director_adapter_clears_a_stale_divergence_note(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(st, "run_director_conversation", _director_returning([]))
    note = _plan_and_ops(tmp_path, "feature — the payoff", [])
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("stale", encoding="utf-8")
    by_name = {s.name: s for s in st.STAGES}
    by_name["director"].run(_ctx(tmp_path, stems=("a",)))
    assert not note.exists()


def test_director_adapter_survives_a_missing_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(st, "run_director_conversation", _director_returning([]))
    by_name = {s.name: s for s in st.STAGES}
    by_name["director"].run(_ctx(tmp_path, stems=("a",)))  # no plan.json, no ops on disk


# --- plan_revise wiring ------------------------------------------------------


def test_plan_revise_sits_between_plan_and_director():
    assert st.STAGE_NAMES.index("plan_revise") == st.STAGE_NAMES.index("plan") + 1
    assert st.STAGE_NAMES.index("plan_revise") == st.STAGE_NAMES.index("director") - 1
    assert [s.name for s in st.STAGES] == st.STAGE_NAMES


def test_plan_revise_requires_no_output(tmp_path):
    """Its absence is meaningful (director falls back to plan/plan.json), so a
    --from-stage director run must not demand the file."""
    stage = next(s for s in st.STAGES if s.name == "plan_revise")
    assert stage.required_outputs(_ctx(tmp_path)) == []


def test_plan_run_is_told_what_to_invalidate(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(st, "run_plan", lambda *a, **kw: seen.update(args=a, kw=kw))
    ctx = _ctx(tmp_path)
    st._plan_run(ctx)
    assert seen["args"][1] == tmp_path / "out" / "plan" / "plan.json"
    assert seen["kw"]["revised"] == tmp_path / "out" / "plan_revise" / "plan.json"
    assert seen["kw"]["history"] == tmp_path / "out" / "plan_dialogue" / "history.md"


def test_plan_revise_run_reads_plan_and_the_conversation(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(st, "run_plan_revise", lambda *a, **kw: seen.update(args=a, kw=kw))
    ctx = _ctx(tmp_path)
    st._plan_revise_run(ctx)
    assert seen["args"][0] == tmp_path / "out" / "summary" / "summary.json"
    assert seen["args"][1] == tmp_path / "out" / "plan" / "plan.json"
    assert seen["args"][2] == tmp_path / "out" / "plan_revise" / "plan.json"
    assert seen["kw"]["history"] == tmp_path / "out" / "plan_dialogue" / "history.md"


def test_director_prefers_the_revised_plan(tmp_path):
    ctx = _ctx(tmp_path)
    plan_json = tmp_path / "out" / "plan" / "plan.json"
    revised = tmp_path / "out" / "plan_revise" / "plan.json"
    assert st._effective_plan_json(ctx) == plan_json
    revised.parent.mkdir(parents=True)
    revised.write_text('{"directions": []}', encoding="utf-8")
    assert st._effective_plan_json(ctx) == revised


def test_divergence_note_is_written_against_the_plan_the_director_read(tmp_path):
    ctx = _ctx(tmp_path, stems=("a",))
    out = tmp_path / "out"
    (out / "plan").mkdir(parents=True)
    (out / "plan" / "plan.json").write_text(
        json.dumps({"directions": [{"stem": "a", "lines": [1, 4], "direction": "feature — x"}]}),
        encoding="utf-8",
    )
    (out / "plan_revise").mkdir(parents=True)
    (out / "plan_revise" / "plan.json").write_text(
        json.dumps({"directions": [{"stem": "a", "lines": [1, 4], "direction": "remove — y"}]}),
        encoding="utf-8",
    )
    (out / "director").mkdir(parents=True)
    (out / "director" / "a_director.json").write_text(
        json.dumps({"ops": [{"type": "keep", "lines": [1, 4], "note": "n"}]}),
        encoding="utf-8",
    )
    st._write_divergence_note(ctx)
    note = (out / "llm_report" / "notes" / "plan_divergence.md").read_text(encoding="utf-8")
    # the revised direction (remove) is the one the director was given
    assert "protected-over-remove" in note
