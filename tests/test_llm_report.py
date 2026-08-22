"""Tests for the structured LLM report writer."""

from __future__ import annotations

import yaml

from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    VERIFY_FAIL,
    Recorder,
    rebuild_index,
)


def _front_matter(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---")
    _, fm, _ = text.split("---", 2)
    return yaml.safe_load(fm)


class TestUnitFile:
    def test_flush_writes_front_matter_and_bodies(self, tmp_path):
        rec = Recorder("director", tmp_path, enabled=True)
        msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USER"},
        ]
        rec.attempt(
            unit="my_video",
            attempt=0,
            total=2,
            messages=msgs,
            response="bad json",
            outcome="unparseable",
            reason="no ops",
            cfg={"temperature": 0.1, "model": "qwen3.5:30b", "thinking": "low"},
        )
        rec.attempt(
            unit="my_video",
            attempt=1,
            total=2,
            messages=msgs,
            response='{"ops": []}',
            outcome=OK_EMPTY,
            cfg={"temperature": 0.3, "model": "qwen3.5:30b", "thinking": "low"},
        )
        rec.flush_unit("my_video", outcome=OK_EMPTY)

        path = tmp_path / "director" / "my_video.md"
        assert path.exists()
        fm = _front_matter(path)
        assert fm["stage"] == "director"
        assert fm["unit"] == "my_video"
        assert fm["attempts"] == 2
        assert fm["outcome"] == OK_EMPTY
        assert fm["model"] == "qwen3.5:30b"
        assert fm["thinking"] == "low"

        body = path.read_text(encoding="utf-8")
        assert "SYS" in body and "USER" in body
        assert "bad json" in body and '{"ops": []}' in body
        assert "temperature 0.1" in body and "temperature 0.3" in body
        assert "no ops" in body  # per-attempt reason rendered

    def test_duration_measured_from_begin_not_first_attempt(self, tmp_path, monkeypatch):
        """Stages record an attempt only AFTER the LLM call returns, so a
        start time taken at first-attempt time misses the whole call and
        every report says duration_ms: 0. begin(unit) marks the real start."""
        import nagare_clip.llm_report as mod

        real_datetime = mod.datetime
        clock = {"now": real_datetime(2026, 1, 1, 12, 0, 0)}

        class FakeDateTime:
            @staticmethod
            def now():
                return clock["now"]

        monkeypatch.setattr(mod, "datetime", FakeDateTime)

        rec = Recorder("director", tmp_path, enabled=True)
        rec.begin("my_video")
        # ... the LLM call takes 3 seconds ...
        clock["now"] = real_datetime(2026, 1, 1, 12, 0, 3)
        rec.attempt(
            unit="my_video",
            attempt=0,
            total=1,
            messages=[],
            response="ok",
            outcome=OK,
            cfg={"model": "m"},
        )
        rec.flush_unit("my_video", outcome=OK)

        fm = _front_matter(tmp_path / "director" / "my_video.md")
        assert fm["duration_ms"] == 3000
        assert fm["started_at"] == "2026-01-01T12:00:00"

    def test_begin_on_disabled_recorder_is_a_noop(self, tmp_path):
        NULL_RECORDER.begin("anything")  # must not raise or accumulate state
        assert NULL_RECORDER._started == {}

    def test_thinking_defaults_to_false_when_omitted(self, tmp_path):
        rec = Recorder("director", tmp_path, enabled=True)
        rec.attempt(
            unit="my_video",
            attempt=0,
            total=1,
            messages=[{"role": "user", "content": "x"}],
            response="y",
            outcome=OK,
            cfg={"temperature": 0.0, "model": "m"},
        )
        rec.flush_unit("my_video", outcome=OK)
        fm = _front_matter(tmp_path / "director" / "my_video.md")
        assert fm["thinking"] is False

    def test_slug_handles_punctuation(self, tmp_path):
        rec = Recorder("text_filter", tmp_path, enabled=True)
        rec.attempt(
            unit="lines 11-20 (size 10)",
            attempt=0,
            total=1,
            messages=[{"role": "user", "content": "x"}],
            response="ok",
            outcome=OK,
            cfg={"temperature": 0.0},
        )
        rec.flush_unit("lines 11-20 (size 10)", outcome=OK)
        files = list((tmp_path / "text_filter").glob("*.md"))
        assert len(files) == 1
        # slug is filesystem-safe (no spaces/parens)
        assert " " not in files[0].name and "(" not in files[0].name
        # human-readable unit preserved in front-matter
        assert _front_matter(files[0])["unit"] == "lines 11-20 (size 10)"


class TestDisabled:
    def test_disabled_recorder_writes_nothing(self, tmp_path):
        rec = Recorder("director", tmp_path, enabled=False)
        rec.attempt(
            unit="u",
            attempt=0,
            total=1,
            messages=[],
            outcome=OK,
        )
        rec.flush_unit("u", outcome=OK)
        assert not (tmp_path / "director").exists()

    def test_null_recorder_is_disabled(self, tmp_path):
        NULL_RECORDER.attempt(
            unit="u",
            attempt=0,
            total=1,
            messages=[],
            outcome=OK,
        )
        NULL_RECORDER.flush_unit("u", outcome=OK)
        # nothing to assert beyond "did not raise"; NULL_RECORDER has no dir
        assert NULL_RECORDER.enabled is False


class TestIndex:
    def _write_unit(self, tmp_path, stage, unit, outcome, reason=""):
        rec = Recorder(stage, tmp_path, enabled=True)
        rec.attempt(
            unit=unit,
            attempt=0,
            total=1,
            messages=[{"role": "user", "content": "x"}],
            response="y",
            outcome=outcome,
            cfg={"temperature": 0.0, "model": "m"},
        )
        rec.flush_unit(unit, outcome=outcome, reason=reason)

    def test_index_lists_all_units_in_stage_order(self, tmp_path):
        self._write_unit(tmp_path, "director", "vid_b", OK)
        self._write_unit(tmp_path, "text_filter", "summary_llm", OK)
        self._write_unit(tmp_path, "guided_edit", "vid_a", DROPPED_ITEMS, "1 op unapplied")

        rebuild_index(tmp_path)
        index = (tmp_path / "index.md").read_text(encoding="utf-8")

        # text_filter row appears before director row (STAGE_ORDER)
        assert index.index("text_filter") < index.index("director")
        assert "summary_llm" in index
        assert "1 op unapplied" in index
        # detail links are relative
        assert "director/vid_b.md" in index
        assert "guided_edit/vid_a.md" in index

    def test_plan_revise_is_listed_between_plan_and_director(self, tmp_path):
        """It runs between them, so an index that sorted it last (an unknown
        stage falls to the end) would read out of pipeline order."""
        self._write_unit(tmp_path, "director", "vid_a", OK)
        self._write_unit(tmp_path, "plan_revise", "plan_revise", OK)
        self._write_unit(tmp_path, "plan", "plan", OK)

        rebuild_index(tmp_path)
        index = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert index.index("| plan ") < index.index("| plan_revise ")
        assert index.index("| plan_revise ") < index.index("| director ")

    def test_index_is_regenerated_not_appended(self, tmp_path):
        self._write_unit(tmp_path, "director", "vid_a", OK)
        rebuild_index(tmp_path)
        # re-run director with a different outcome (refresh own section)
        Recorder("director", tmp_path, enabled=True).clear()
        self._write_unit(tmp_path, "director", "vid_a", VERIFY_FAIL)
        rebuild_index(tmp_path)

        index = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert index.count("director/vid_a.md") == 1
        assert VERIFY_FAIL in index

    def test_separate_processes_accumulate_when_second_does_not_clear(self, tmp_path):
        # process 1: clears, writes vid_a
        r1 = Recorder("director", tmp_path, enabled=True)
        r1.clear()
        r1.attempt(
            unit="vid_a",
            attempt=0,
            total=1,
            messages=[{"role": "user", "content": "x"}],
            response="y",
            outcome=OK,
            cfg={"temperature": 0.0, "model": "m"},
        )
        r1.flush_unit("vid_a", outcome=OK)
        # process 2: does NOT clear (simulates a later loop iteration), writes vid_b
        r2 = Recorder("director", tmp_path, enabled=True)
        r2.attempt(
            unit="vid_b",
            attempt=0,
            total=1,
            messages=[{"role": "user", "content": "x"}],
            response="y",
            outcome=OK,
            cfg={"temperature": 0.0, "model": "m"},
        )
        r2.flush_unit("vid_b", outcome=OK)
        rebuild_index(tmp_path)
        assert (tmp_path / "director" / "vid_a.md").exists()
        assert (tmp_path / "director" / "vid_b.md").exists()
        index = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert "vid_a" in index and "vid_b" in index


class TestDeterministicRecords:
    def test_deterministic_attempt_renders_without_temperature_or_attempt_count(self, tmp_path):
        from nagare_clip.llm_report import OK, Recorder

        rec = Recorder("guided_edit", tmp_path)
        rec.attempt(
            unit="u",
            attempt=0,
            total=1,
            messages=[],
            response="<keep>x</keep>",
            outcome=OK,
            deterministic=True,
        )
        rec.flush_unit("u", outcome=OK)
        text = (tmp_path / "guided_edit" / "u.md").read_text(encoding="utf-8")
        assert "deterministic — ok" in text
        assert "temperature" not in text
        assert "Attempt" not in text

    def test_llm_attempt_still_renders_attempt_header(self, tmp_path):
        from nagare_clip.llm_report import OK, Recorder

        rec = Recorder("guided_edit", tmp_path)
        rec.attempt(
            unit="u",
            attempt=0,
            total=2,
            messages=[{"role": "user", "content": "hi"}],
            response="ok",
            outcome=OK,
            cfg={"temperature": 0.4, "model": "m1"},
        )
        rec.flush_unit("u", outcome=OK)
        text = (tmp_path / "guided_edit" / "u.md").read_text(encoding="utf-8")
        assert "Attempt 1/2 — temperature 0.4 — ok" in text
        assert "model: m1" in text

    def test_front_matter_model_prefers_real_llm_attempt(self, tmp_path):
        """A unit whose LAST record is deterministic must still report the
        model of the LLM attempt that actually ran."""
        from nagare_clip.llm_report import OK, Recorder

        rec = Recorder("guided_edit", tmp_path)
        rec.attempt(
            unit="u",
            attempt=0,
            total=1,
            messages=[],
            response="r",
            outcome=OK,
            cfg={"temperature": 0.1, "model": "m1"},
        )
        rec.attempt(
            unit="u", attempt=0, total=1, messages=[], response="r2", outcome=OK, deterministic=True
        )
        rec.flush_unit("u", outcome=OK)
        text = (tmp_path / "guided_edit" / "u.md").read_text(encoding="utf-8")
        assert "model: m1" in text


class TestIndexNotes:
    """notes/*.md hold deterministic findings (no LLM call) that must survive
    the next stage's rebuild of the index."""

    def test_note_is_inlined_after_the_table(self, tmp_path):
        (tmp_path / "notes").mkdir(parents=True)
        (tmp_path / "notes" / "plan_divergence.md").write_text(
            "## plan/director divergence\n\n- v [1-10] argued with\n", encoding="utf-8"
        )
        rebuild_index(tmp_path)
        index = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert "## plan/director divergence" in index
        assert index.index("| Stage |") < index.index("## plan/director divergence")

    def test_notes_are_not_listed_as_calls(self, tmp_path):
        (tmp_path / "notes").mkdir(parents=True)
        (tmp_path / "notes" / "plan_divergence.md").write_text("## x\n", encoding="utf-8")
        rebuild_index(tmp_path)
        index = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert "0 call(s)" in index

    def test_no_notes_dir_is_unchanged(self, tmp_path):
        rebuild_index(tmp_path)
        assert "## " not in (tmp_path / "index.md").read_text(encoding="utf-8").split("\n", 1)[1]
