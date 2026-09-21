"""The plan stage refuses to retire turns nobody has applied yet.

Re-running ``plan`` appends a divider to ``plan_dialogue/history.md``, and the
turns above a divider stop being applied.  When the human's last turn has not
been answered, that divider throws away instructions that cost a human real
work -- silently, because ``plan_revise`` then reports nothing unanswered and
makes no call.  The window is checked *before* any stage runs, so an earlier
``--from-stage`` does not spend a transcription run on its way to the loss.
"""

from __future__ import annotations

import pytest
import yaml

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline import cli
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia

PLAN = st.STAGE_NAMES.index("plan")
REVISE = st.STAGE_NAMES.index("plan_revise")
DIRECTOR = st.STAGE_NAMES.index("director")


def _ctx(tmp_path, *, from_index=PLAN, to_index=None):
    return PipelineContext(
        cfg=get_effective_config(None, {}),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=[SourceMedia(abs_path=tmp_path / "a.mp4", stem="a", relative="a.mp4")],
        from_index=from_index,
        to_index=len(st.STAGE_NAMES) - 1 if to_index is None else to_index,
    )


def _history(tmp_path, text: str):
    path = tmp_path / "out" / "plan_dialogue" / "history.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


UNANSWERED = (
    "## plan\n\nfirst account\n\n"
    "--- plan re-ran 2026-08-24T01:56 — turns above this line no longer apply ---\n\n"
    "## plan\n\nsecond account\n\n"
    "## human\n\nsplit part 21 at line 60\n"
)
ANSWERED = UNANSWERED + "\n## plan\n\ndone, split at 60\n"


class TestGuard:
    def test_raises_when_plan_would_retire_an_unanswered_turn(self, tmp_path):
        _history(tmp_path, UNANSWERED)
        with pytest.raises(PipelineError) as e:
            st.check_unanswered_turns(_ctx(tmp_path))
        assert "history.md" in str(e.value)

    def test_error_counts_the_turns_the_divider_would_retire(self, tmp_path):
        # Two turns live below the last divider -- the plan account and the
        # human's instruction -- and a new divider retires both.
        _history(tmp_path, UNANSWERED)
        with pytest.raises(PipelineError) as e:
            st.check_unanswered_turns(_ctx(tmp_path))
        assert "2 turn" in str(e.value)

    def test_error_names_both_ways_out(self, tmp_path):
        _history(tmp_path, UNANSWERED)
        with pytest.raises(PipelineError) as e:
            st.check_unanswered_turns(_ctx(tmp_path))
        message = str(e.value)
        assert "--from-stage plan_revise --to-stage plan_revise" in message
        assert "--retire-turns" in message

    def test_silent_when_the_conversation_is_answered(self, tmp_path):
        _history(tmp_path, ANSWERED)
        st.check_unanswered_turns(_ctx(tmp_path))

    def test_silent_when_there_is_no_history_file(self, tmp_path):
        st.check_unanswered_turns(_ctx(tmp_path))

    def test_silent_when_plan_is_before_the_window(self, tmp_path):
        # --from-stage plan_revise is the fix the error recommends; it must not
        # trip the guard it was recommended by.
        _history(tmp_path, UNANSWERED)
        st.check_unanswered_turns(_ctx(tmp_path, from_index=REVISE))

    def test_silent_when_plan_is_after_the_window(self, tmp_path):
        _history(tmp_path, UNANSWERED)
        st.check_unanswered_turns(_ctx(tmp_path, from_index=0, to_index=PLAN - 1))

    def test_fires_when_plan_is_inside_a_wider_window(self, tmp_path):
        # The run that caused the loss: --from-stage plan --to-stage blender,
        # and equally --from-stage summary, which reaches plan on its way.
        _history(tmp_path, UNANSWERED)
        with pytest.raises(PipelineError):
            st.check_unanswered_turns(_ctx(tmp_path, from_index=0, to_index=DIRECTOR))

    def test_retire_turns_allows_the_run(self, tmp_path):
        _history(tmp_path, UNANSWERED)
        st.check_unanswered_turns(_ctx(tmp_path), retire_turns=True)


class TestCli:
    def _project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "src_video").mkdir()
        (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
        cfg = tmp_path / "c.yml"
        cfg.write_text(yaml.safe_dump({"pipeline": {"output_dir": str(tmp_path / "output")}}))
        return cfg

    def test_retire_turns_defaults_to_false(self):
        assert cli.parse_args([]).retire_turns is False

    def test_retire_turns_flag_parses(self):
        assert cli.parse_args(["--retire-turns"]).retire_turns is True

    def test_retire_turns_is_not_a_config_override(self):
        # It decides whether this one run may throw turns away; it is not a
        # project setting that could sit forgotten in a YAML file.
        assert cli.build_cli_overrides(cli.parse_args(["--retire-turns"])) == {}

    def test_main_stops_before_any_stage_runs(self, tmp_path, monkeypatch, capsys):
        cfg = self._project(tmp_path, monkeypatch)
        (tmp_path / "output" / "plan_dialogue").mkdir(parents=True, exist_ok=True)
        (tmp_path / "output" / "plan_dialogue" / "history.md").write_text(
            UNANSWERED, encoding="utf-8"
        )
        ran = []
        monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: ran.append(ctx))
        rc = cli.main(["--config", str(cfg), "--from-stage", "plan", "--to-stage", "blender"])
        assert rc == 1
        assert ran == []
        err = capsys.readouterr().err
        assert "--retire-turns" in err

    def test_main_runs_with_retire_turns(self, tmp_path, monkeypatch):
        cfg = self._project(tmp_path, monkeypatch)
        (tmp_path / "output" / "plan_dialogue").mkdir(parents=True, exist_ok=True)
        (tmp_path / "output" / "plan_dialogue" / "history.md").write_text(
            UNANSWERED, encoding="utf-8"
        )
        ran = []
        monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: ran.append(ctx))
        rc = cli.main(
            ["--config", str(cfg), "--from-stage", "plan", "--to-stage", "plan", "--retire-turns"]
        )
        assert rc == 0
        assert len(ran) == 1
