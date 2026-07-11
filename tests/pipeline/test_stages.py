"""Tests for the real STAGES registry wiring."""

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


def _ctx(tmp_path, cfg=None, stems=("a", "b"), from_index=0, to_index=9):
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
        to_index=to_index,
    )


def test_stage_names_canonical_order():
    assert st.STAGE_NAMES == [
        "transcription",
        "audio_silence",
        "sentence_split",
        "text_filter",
        "summary",
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
