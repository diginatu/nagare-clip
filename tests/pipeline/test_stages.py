"""Tests for the real STAGES registry wiring."""

import json
import shlex
import subprocess

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.errors import PipelineError
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
        "audio_silence",
        "transcription",
        "whisper",
        "sentence_split",
        "gap_context",
        "summary",
        "text_filter",
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
    req = st.STAGES[1].required_outputs(ctx)
    assert (tmp_path / "out" / "transcription" / "a.json") in req
    assert (tmp_path / "out" / "transcription" / "b.txt") in req
    # starting after sentence_split: transcription outputs not needed
    ctx2 = _ctx(tmp_path, from_index=st.STAGE_NAMES.index("text_filter"))
    assert st.STAGES[1].required_outputs(ctx2) == []


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
        lambda txts, out, cfg, *, json_paths=None, gaps_paths=None, recorder=None: seen.update(
            txts=txts,
            out=out,
            json_paths=json_paths,
            gaps_paths=gaps_paths,
        ),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["summary"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen["txts"] == [out / "sentence_split" / "a.txt"]
    assert seen["json_paths"] == [out / "sentence_split" / "a.json"]
    assert seen["gaps_paths"] == [out / "gap_context" / "a_gaps.json"]
    assert seen["out"] == out / "summary" / "summary.json"


def test_text_filter_adapter_passes_summary_json(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_text_filter",
        lambda txt, out, cfg, *, summary_json=None, recorder=None, hints=None: seen.append(
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
        commands.append((cmd, kwargs))
        _materialise_frames_from_script(gap_ctx.output_dir, kwargs["stdin_text"])

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
    assert commands[0][1]["stdin_text"].count("ffmpeg ") == 5

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


def test_gap_context_docker_failure_stops_the_pipeline(gap_ctx, monkeypatch):
    """If the ONE docker call fails (docker missing, image gone, non-zero
    exit), the stage raises PipelineError -- the run stops there instead of
    describing zero gaps and carrying on. No gaps file is written, so the
    next run redoes the stage."""
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("10.000 - 20.000\n", encoding="utf-8")

    def flaky(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    called = []
    monkeypatch.setattr(stages_mod, "run_command", flaky)
    monkeypatch.setattr(stages_mod, "run_gap_context", lambda *a, **k: called.append(a))
    gap_ctx.cfg["gap_context"]["enabled"] = True

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    with pytest.raises(PipelineError):
        stage.run(gap_ctx)
    assert called == []
    assert not (gap_ctx.stage_dir("gap_context") / "talk1_gaps.json").exists()


def test_gap_context_a_single_bad_line_does_not_drop_other_frames(gap_ctx, monkeypatch):
    """`|| true` on each ffmpeg line means one failed seek inside the batch
    must not take out the other frames/gaps sharing the same container."""
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("10.000 - 20.000\n", encoding="utf-8")

    def fake_run_command(cmd, **kwargs):
        script = kwargs["stdin_text"]
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
        commands.append((cmd, kwargs))
        _materialise_frames_from_script(ctx.output_dir, kwargs["stdin_text"])

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
    assert commands[0][1]["stdin_text"].count("ffmpeg ") == 10

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
        commands.append((cmd, kwargs))
        _materialise_frames_from_script(gap_ctx.output_dir, kwargs["stdin_text"])

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
        commands.append((cmd, kwargs))
        _materialise_frames_from_script(gap_ctx.output_dir, kwargs["stdin_text"])

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)
    gap_ctx.cfg["gap_context"]["static_ssim"] = 0

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(10.0, 20.0)])])

    script = commands[0][1]["stdin_text"]
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
        commands.append((cmd, kwargs))
        script = kwargs["stdin_text"]
        _materialise_frames_from_script(gap_ctx.output_dir, script)
        _write_ssim_stats_for_pairs(gap_ctx, script, [0.998, 0.996132])

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(10.0, 20.0)])])

    script = commands[0][1]["stdin_text"]
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
        commands.append((cmd, kwargs))
        script = kwargs["stdin_text"]
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
        commands.append((cmd, kwargs))
        script = kwargs["stdin_text"]
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
        commands.append((cmd, kwargs))
        script = kwargs["stdin_text"]
        _materialise_frames_from_script(gap_ctx.output_dir, script)

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)

    src = gap_ctx.sources[0]
    result = stages_mod._extract_gap_frames(gap_ctx, [(src, [(start, end)])])

    script = commands[0][1]["stdin_text"]
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
        commands.append((cmd, kwargs))
        script = kwargs["stdin_text"]
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
        commands.append((cmd, kwargs))
        script = kwargs["stdin_text"]
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


def test_gap_context_batch_script_goes_to_stdin_not_argv(gap_ctx, monkeypatch):
    """The script must never ride in argv (MAX_ARG_STRLEN is 128 KiB)."""
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("10.000 - 20.000\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(stages_mod, "run_command", lambda cmd, **k: calls.append((cmd, k)))
    monkeypatch.setattr(stages_mod, "run_gap_context", lambda *a, **k: None)
    gap_ctx.cfg["gap_context"]["enabled"] = True
    next(s for s in stages_mod.STAGES if s.name == "gap_context").run(gap_ctx)

    ((cmd, kwargs),) = calls
    assert kwargs["stdin_text"].count("ffmpeg ") == 5
    assert not any("ffmpeg" in a for a in cmd)
    assert kwargs.get("capture_stderr") is True


def test_gap_context_docker_failure_error_names_the_reason(gap_ctx, monkeypatch):
    """A bare "batch frame extraction failed" once hid an `Argument list too
    long` for a whole run. The error must carry the exception text and the
    process's stderr."""
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("10.000 - 20.000\n", encoding="utf-8")
    monkeypatch.setattr(stages_mod, "run_gap_context", lambda *a, **k: None)
    gap_ctx.cfg["gap_context"]["enabled"] = True
    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")

    def boom(cmd, **kwargs):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(stages_mod, "run_command", boom)
    with pytest.raises(PipelineError) as exc:
        stage.run(gap_ctx)
    assert "gap_context" in str(exc.value)
    assert "Argument list too long" in str(exc.value)

    def failed(cmd, **kwargs):
        raise subprocess.CalledProcessError(125, cmd, stderr="Error: No such image: whisperx\n")

    monkeypatch.setattr(stages_mod, "run_command", failed)
    with pytest.raises(PipelineError) as exc:
        stage.run(gap_ctx)
    assert "exit status 125" in str(exc.value)
    assert "No such image: whisperx" in str(exc.value)


def test_audio_silence_stage_one_vad_call_for_all_sources(tmp_path, monkeypatch):
    from nagare_clip.audio_silence.cuts_file import read_cuts

    ctx = _ctx(tmp_path, stems=("a", "b"))
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        req = json.loads(open(cmd[-1]).read())
        for s in req["sources"]:
            with open(s["out"], "w") as f:
                json.dump({"duration": 6.0, "speech": [[1.0, 2.0], [2.2, 5.0]]}, f)

    monkeypatch.setattr(st, "run_command", fake_run)
    st._audio_silence_run(ctx)

    assert len(calls) == 1
    d = tmp_path / "out" / "audio_silence"
    cmd, kw = calls[0]
    assert cmd == [
        "uv", "run", "--project", str(tmp_path / "transcribe"), "python", "-m",
        "nagare_transcribe", "vad", str(d / "vad_request.json"),
    ]  # fmt: skip
    assert "env_extra" not in kw
    req = json.loads((d / "vad_request.json").read_text())
    a = ctx.cfg["audio_silence"]
    assert req["params"] == {
        "threshold": a["threshold"],
        "min_speech": a["min_speech"],
        "min_silence": a["min_silence"],
        "speech_pad": a["speech_pad"],
    }
    assert req["sources"] == [
        {"path": str(s.abs_path), "out": str(d / f"{s.stem}_vad.json")} for s in ctx.sources
    ]
    for stem in ("a", "b"):
        assert read_cuts(d / f"{stem}_cuts.txt") == [(0.0, 1.0), (5.0, 6.0)]


def test_stage_order_audio_silence_first():
    assert st.STAGE_NAMES[:3] == ["audio_silence", "transcription", "whisper"]


def test_transcription_stage_one_asr_call_for_all_sources(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, stems=("a", "b"))
    adir = tmp_path / "out" / "audio_silence"
    adir.mkdir(parents=True)
    for stem in ("a", "b"):
        (adir / f"{stem}_vad.json").write_text(
            json.dumps({"duration": 10.0, "speech": [[1.0, 2.0], [2.5, 4.0]]})
        )
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        req = json.loads(open(cmd[-1]).read())
        for s in req["sources"]:
            with open(s["out"], "w") as f:
                json.dump(
                    {
                        "chunks": s["chunks"],
                        "tokens": [{"text": "あい", "start": 1.0, "end": 1.4}],
                    },
                    f,
                )

    monkeypatch.setattr(st, "run_command", fake_run)
    st._transcription_run(ctx)

    d = tmp_path / "out" / "transcription"
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd == [
        "uv", "run", "--project", str(tmp_path / "transcribe"), "python", "-m",
        "nagare_transcribe", "asr", str(d / "asr_request.json"),
    ]  # fmt: skip
    assert "env_extra" not in kw
    req = json.loads((d / "asr_request.json").read_text())
    t = ctx.cfg["transcription"]
    assert req["model"] == t["model"]
    assert req["batch_size"] == t["batch_size"]
    assert req["max_chunk_seconds"] == t["max_chunk_seconds"]
    assert [s["path"] for s in req["sources"]] == [str(s.abs_path) for s in ctx.sources]
    assert [s["out"] for s in req["sources"]] == [str(d / f"{s}_haas.json") for s in ("a", "b")]
    for stem in ("a", "b"):
        data = json.loads((d / f"{stem}.json").read_text())
        assert data["segments"][0]["text"] == "あい"
        assert (d / f"{stem}.txt").read_text().strip() == "あい"


def test_transcription_stage_joins_regions_by_audio_silence_min_silence(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, stems=("a",))
    ctx.cfg["audio_silence"]["min_silence"] = 0.7
    adir = tmp_path / "out" / "audio_silence"
    adir.mkdir(parents=True)
    (adir / "a_vad.json").write_text(
        json.dumps({"duration": 10.0, "speech": [[1.0, 2.0], [2.6, 3.0]]})
    )

    def fake_run(cmd, **kw):
        req = json.loads(open(cmd[-1]).read())
        assert req["sources"][0]["chunks"] == [[1.0, 3.0]]
        with open(req["sources"][0]["out"], "w") as f:
            json.dump(
                {
                    "chunks": req["sources"][0]["chunks"],
                    "tokens": [
                        {"text": "あ", "start": 1.0, "end": 1.4},
                        {"text": "い", "start": 2.6, "end": 3.0},
                    ],
                },
                f,
            )

    monkeypatch.setattr(st, "run_command", fake_run)
    st._transcription_run(ctx)
    d = tmp_path / "out" / "transcription"
    assert json.loads((d / "a.json").read_text())["segments"][0]["text"] == "あい"
    assert (d / "a.txt").read_text().splitlines() == ["あい"]


def test_from_stage_transcription_requires_vad_json(tmp_path):
    by_name = {s.name: s for s in st.STAGES}
    d = tmp_path / "out" / "audio_silence"
    at = _ctx(tmp_path, from_index=st.STAGE_NAMES.index("transcription"))
    assert d / "a_vad.json" in by_name["audio_silence"].required_outputs(at)
    after = _ctx(tmp_path, from_index=st.STAGE_NAMES.index("sentence_split"))
    assert d / "a_vad.json" not in by_name["audio_silence"].required_outputs(after)


# --- whisper -----------------------------------------------------------------


def _whisper_cfg(enabled=True):
    return get_effective_config(None, {"whisper": {"enabled": enabled}})


def test_stage_order_has_whisper_after_transcription():
    i = st.STAGE_NAMES.index("transcription")
    assert st.STAGE_NAMES[i + 1] == "whisper"
    assert st.STAGE_NAMES[i + 2] == "sentence_split"


def test_whisper_disabled_no_docker_call(tmp_path, monkeypatch, capsys):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(st, "run_command", lambda *a, **k: pytest.fail("docker called"))
    st._whisper_run(ctx)
    assert "skip" in capsys.readouterr().out.lower()
    assert not (tmp_path / "out" / "whisper").exists()


def test_whisper_missing_haas_json_raises(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, cfg=_whisper_cfg())
    monkeypatch.setattr(st, "run_command", lambda *a, **k: pytest.fail("docker called"))
    with pytest.raises(st.PipelineError, match=r"_haas\.json.*--from-stage transcription"):
        st._whisper_run(ctx)


def test_whisper_warns_when_hints_unused(tmp_path, monkeypatch, capsys):
    ctx = _ctx(tmp_path, cfg=_whisper_cfg())
    tdir = tmp_path / "out" / "transcription"
    tdir.mkdir(parents=True)
    for stem in ctx.stems:
        (tdir / f"{stem}_haas.json").write_text(json.dumps({"chunks": []}))
    monkeypatch.setattr(st, "run_command", lambda *a, **k: None)
    st._whisper_run(ctx)
    assert "use_llm" in capsys.readouterr().out


def test_whisper_stage_one_docker_call_for_all_sources(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, cfg=_whisper_cfg(), stems=("a", "b"))
    tdir = tmp_path / "out" / "transcription"
    tdir.mkdir(parents=True)
    (tdir / "a_haas.json").write_text(json.dumps({"chunks": [[0.0, 2.0], [3.0, 5.5]]}))
    (tdir / "b_haas.json").write_text(json.dumps({"chunks": []}))
    calls = []
    monkeypatch.setattr(st, "run_command", lambda cmd, **kw: calls.append((cmd, kw)))
    st._whisper_run(ctx)

    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd[-2:] == ["-", "/output/whisper/whisper_request.json"]
    assert "--entrypoint" in cmd and "python" in cmd
    assert kw["env_extra"] == st._docker_env(ctx)
    assert "faster_whisper" in kw["stdin_text"]
    req = json.loads((tmp_path / "out" / "whisper" / "whisper_request.json").read_text())
    assert req == {
        "model": "large-v3",
        "beam_size": 5,
        "sources": [
            {
                "path": "/app/a.mp4",
                "out": "/output/whisper/a_whisper.json",
                "chunks": [[0.0, 2.0], [3.0, 5.5]],
            },
            {"path": "/app/b.mp4", "out": "/output/whisper/b_whisper.json", "chunks": []},
        ],
    }


def test_whisper_required_only_when_enabled(tmp_path):
    w = next(s for s in st.STAGES if s.name == "whisper")
    tf = st.STAGE_NAMES.index("text_filter")
    off = _ctx(tmp_path, from_index=tf)
    assert w.required_outputs(off) == []
    on = _ctx(tmp_path, cfg=_whisper_cfg(), from_index=tf)
    d = tmp_path / "out" / "whisper"
    assert w.required_outputs(on) == [d / "a_whisper.json", d / "b_whisper.json"]
    after = _ctx(tmp_path, cfg=_whisper_cfg(), from_index=tf + 1)
    assert w.required_outputs(after) == []


def _tf_setup(tmp_path, monkeypatch, cfg, lines=2):
    seen = []
    monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _NullRec())
    monkeypatch.setattr(
        st,
        "run_text_filter",
        lambda txt, out, cfg, *, summary_json=None, recorder=None, hints=None: seen.append(hints),
    )
    out = tmp_path / "out"
    (out / "sentence_split").mkdir(parents=True)
    (out / "whisper").mkdir(parents=True)
    (out / "sentence_split" / "a.txt").write_text("\n".join(["x"] * lines) + "\n")
    (out / "sentence_split" / "a.json").write_text("{}")
    (out / "whisper" / "a_whisper.json").write_text("{}")
    return seen


def test_text_filter_ignores_stale_whisper_when_disabled(tmp_path, monkeypatch):
    seen = _tf_setup(tmp_path, monkeypatch, None)
    monkeypatch.setattr(st, "load_line_hints", lambda *a: pytest.fail("hints loaded"))
    st._text_filter_run(_ctx(tmp_path, stems=("a",)))
    assert seen == [None]


def test_text_filter_passes_hints_when_enabled(tmp_path, monkeypatch):
    seen = _tf_setup(tmp_path, monkeypatch, None)
    args = []
    monkeypatch.setattr(st, "load_line_hints", lambda *a: args.append(a) or ["h", None])
    st._text_filter_run(_ctx(tmp_path, cfg=_whisper_cfg(), stems=("a",)))
    out = tmp_path / "out"
    assert seen == [["h", None]]
    assert args == [(out / "sentence_split" / "a.json", out / "whisper" / "a_whisper.json")]


def test_text_filter_drops_hints_on_line_count_mismatch(tmp_path, monkeypatch, caplog):
    seen = _tf_setup(tmp_path, monkeypatch, None, lines=3)
    monkeypatch.setattr(st, "load_line_hints", lambda *a: ["h", None])
    st._text_filter_run(_ctx(tmp_path, cfg=_whisper_cfg(), stems=("a",)))
    assert seen == [None]
    assert "hint" in caplog.text.lower()
