"""Tests for the structured LLM report writer."""

from __future__ import annotations

import yaml

from nagare_clip.llm_report import (
    DROPPED_ITEMS,
    LLM_ERROR,
    NULL_RECORDER,
    OK,
    OK_EMPTY,
    Recorder,
    recorder_from_config,
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
            unit="my_video", attempt=0, total=2, messages=msgs,
            response="bad json", outcome="unparseable", reason="no ops",
            cfg={"temperature": 0.1, "model": "qwen3.5:30b"},
        )
        rec.attempt(
            unit="my_video", attempt=1, total=2, messages=msgs,
            response='{"ops": []}', outcome=OK_EMPTY,
            cfg={"temperature": 0.3, "model": "qwen3.5:30b"},
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

        body = path.read_text(encoding="utf-8")
        assert "SYS" in body and "USER" in body
        assert "bad json" in body and '{"ops": []}' in body
        assert "temperature 0.1" in body and "temperature 0.3" in body
        assert "no ops" in body  # per-attempt reason rendered

    def test_slug_handles_punctuation(self, tmp_path):
        rec = Recorder("text_filter", tmp_path, enabled=True)
        rec.attempt(
            unit="lines 11-20 (size 10)", attempt=0, total=1,
            messages=[{"role": "user", "content": "x"}],
            response="ok", outcome=OK, cfg={"temperature": 0.0},
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
            unit="u", attempt=0, total=1, messages=[], outcome=OK,
        )
        rec.flush_unit("u", outcome=OK)
        assert not (tmp_path / "director").exists()

    def test_null_recorder_is_disabled(self, tmp_path):
        NULL_RECORDER.attempt(
            unit="u", attempt=0, total=1, messages=[], outcome=OK,
        )
        NULL_RECORDER.flush_unit("u", outcome=OK)
        # nothing to assert beyond "did not raise"; NULL_RECORDER has no dir
        assert NULL_RECORDER.enabled is False
