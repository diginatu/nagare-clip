"""plan and plan_revise write the order into their artifacts."""

from __future__ import annotations

import json

import nagare_clip.plan.run as plan_run
import nagare_clip.plan_revise.run as revise_run
from nagare_clip.order import Segment
from nagare_clip.plan.plan_llm import ParsedPlan
from nagare_clip.plan_revise.revise_llm import Revision
from nagare_clip.summary.summarize import PartSummary, ProjectSummary, summary_to_dict

PARTS = [PartSummary("dev", (1, 40), "装置"), PartSummary("mix", (1, 97), "混在")]
COUNTS = {"dev": 40, "mix": 97}
REORDER = [Segment("mix", (31, 97)), Segment("dev", None), Segment("mix", (1, 30))]


def _summary(tmp_path):
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary_to_dict(ProjectSummary("全体", PARTS))), encoding="utf-8")
    return path


class TestPlanRun:
    def test_the_order_is_written_into_plan_json(self, tmp_path, monkeypatch):
        seen = {}

        def fake(project, cfg, **kw):
            seen.update(kw)
            return ParsedPlan([], "並べ替えました", REORDER)

        monkeypatch.setattr(plan_run, "generate_plan", fake)
        out = tmp_path / "plan.json"
        plan_run.run_plan(_summary(tmp_path), out, {"plan": {"enabled": True}}, line_counts=COUNTS)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["order"] == [
            {"stem": "mix", "lines": [31, 97]},
            {"stem": "dev"},
            {"stem": "mix", "lines": [1, 30]},
        ]

    def test_the_line_counts_reach_the_llm(self, tmp_path, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            plan_run,
            "generate_plan",
            lambda project, cfg, **kw: (seen.update(kw), ParsedPlan([], "m"))[1],
        )
        plan_run.run_plan(
            _summary(tmp_path),
            tmp_path / "plan.json",
            {"plan": {"enabled": True}},
            line_counts=COUNTS,
        )
        assert seen["line_counts"] == COUNTS

    def test_disabled_writes_no_order(self, tmp_path):
        out = tmp_path / "plan.json"
        plan_run.run_plan(_summary(tmp_path), out, {"plan": {"enabled": False}})
        assert "order" not in json.loads(out.read_text(encoding="utf-8"))


class TestPlanReviseRun:
    def _history(self, tmp_path):
        path = tmp_path / "history.md"
        path.write_text("## human\n装置を先に\n", encoding="utf-8")
        return path

    def _plan(self, tmp_path, order=None):
        path = tmp_path / "plan.json"
        payload = {"directions": []}
        if order is not None:
            payload["order"] = order
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_the_current_order_reaches_the_llm(self, tmp_path, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            revise_run,
            "generate_revision",
            lambda p, d, t, cfg, **kw: (seen.update(kw), Revision([], [], "m", "", True))[1],
        )
        revise_run.run_plan_revise(
            _summary(tmp_path),
            self._plan(tmp_path, [{"stem": "mix"}, {"stem": "dev"}]),
            tmp_path / "revised.json",
            {"plan_revise": {"enabled": True}},
            history=self._history(tmp_path),
            line_counts=COUNTS,
        )
        assert seen["order"] == [Segment("mix", None), Segment("dev", None)]
        assert seen["line_counts"] == COUNTS

    def test_the_restated_order_is_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            revise_run,
            "generate_revision",
            lambda p, d, t, cfg, **kw: Revision([], REORDER, "並べ替え", "", True),
        )
        out = tmp_path / "revised.json"
        revise_run.run_plan_revise(
            _summary(tmp_path),
            self._plan(tmp_path),
            out,
            {"plan_revise": {"enabled": True}},
            history=self._history(tmp_path),
            line_counts=COUNTS,
        )
        assert len(json.loads(out.read_text(encoding="utf-8"))["order"]) == 3

    def test_an_inherited_order_is_carried_into_the_revised_artifact(self, tmp_path, monkeypatch):
        # plan_revise/plan.json is the file director reads, so it must carry the
        # effective order even when the revision did not touch it.
        current = [
            {"stem": "mix", "lines": [31, 97]},
            {"stem": "dev"},
            {"stem": "mix", "lines": [1, 30]},
        ]

        def fake(p, d, t, cfg, **kw):
            return Revision([], list(kw["order"]), "m", "", True)

        monkeypatch.setattr(revise_run, "generate_revision", fake)
        out = tmp_path / "revised.json"
        revise_run.run_plan_revise(
            _summary(tmp_path),
            self._plan(tmp_path, current),
            out,
            {"plan_revise": {"enabled": True}},
            history=self._history(tmp_path),
            line_counts=COUNTS,
        )
        assert json.loads(out.read_text(encoding="utf-8"))["order"] == current


class TestTheAdapters:
    """The orchestrator is where the line counts come from."""

    def _project(self, tmp_path):
        (tmp_path / "in").mkdir()
        tf = tmp_path / "out" / "text_filter"
        tf.mkdir(parents=True)
        for stem, count in COUNTS.items():
            (tmp_path / "in" / f"{stem}.mp4").touch()
            (tf / f"{stem}_edits.txt").write_text("x\n" * count, encoding="utf-8")
        return tmp_path

    def _ctx(self, tmp_path):
        from nagare_clip.config import get_effective_config
        from nagare_clip.pipeline.runner import PipelineContext
        from nagare_clip.pipeline.sources import SourceMedia

        return PipelineContext(
            cfg=get_effective_config(None, {}),
            project_root=tmp_path,
            config_path=None,
            input_videos_dir=tmp_path / "in",
            output_dir=tmp_path / "out",
            sources=[
                SourceMedia(abs_path=tmp_path / f"{s}.mp4", stem=s, relative=f"{s}.mp4")
                for s in COUNTS
            ],
            from_index=0,
            to_index=12,
        )

    def _seen(self, tmp_path, monkeypatch, stage, target):
        from nagare_clip.pipeline import stages as st

        seen = {}

        class _Rec:
            stage = "x"

            def clear(self): ...
            def rebuild_index(self): ...

        monkeypatch.setattr(st, "recorder_from_config", lambda *a, **k: _Rec())
        monkeypatch.setattr(st, target, lambda *a, **kw: seen.update(kw))
        next(s for s in st.STAGES if s.name == stage).run(self._ctx(self._project(tmp_path)))
        return seen

    def test_the_plan_adapter_passes_the_line_counts(self, tmp_path, monkeypatch):
        seen = self._seen(tmp_path, monkeypatch, "plan", "run_plan")
        assert seen["line_counts"] == COUNTS

    def test_the_plan_revise_adapter_passes_the_line_counts(self, tmp_path, monkeypatch):
        seen = self._seen(tmp_path, monkeypatch, "plan_revise", "run_plan_revise")
        assert seen["line_counts"] == COUNTS
