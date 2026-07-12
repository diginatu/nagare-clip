"""Tests for run_text_filter: summary.json context + keyword injection."""

from __future__ import annotations

import json
from unittest.mock import patch

from nagare_clip.config import get_effective_config
from nagare_clip.text_filter.run import run_text_filter


def _s2_config(constant_keywords: list | None = None) -> dict:
    return {
        "use_llm": True,
        "api_base": "http://localhost:11434",
        "model": "test",
        "api_key": "",
        "batch_size": 10,
        "timeout": 60,
        "retry_on_invalid": False,
        "retry_min_batch_size": 1,
        "prompt": "Base prompt.",
        "temperature": 0.1,
        "thinking": False,
        "keywords": constant_keywords if constant_keywords is not None else [],
    }


def _run(tmp_path, s2_config, summary_data=None, lines=None):
    """Run run_text_filter and return the filter_cfg passed to filter_transcript."""
    if lines is None:
        lines = ["test line"]

    txt = tmp_path / "test.txt"
    txt.write_text("\n".join(lines))
    output = tmp_path / "test_edits.txt"

    summary_json = None
    if summary_data is not None:
        summary_json = tmp_path / "summary.json"
        summary_json.write_text(
            summary_data if isinstance(summary_data, str) else json.dumps(summary_data),
            encoding="utf-8",
        )

    captured: dict = {}

    def mock_filter(lines, cfg, **kwargs):
        captured.update(cfg)
        return lines

    config = {
        "general": {"log_level": "WARNING", "log_file": ""},
        "text_filter": s2_config,
    }

    with patch("nagare_clip.text_filter.run.filter_transcript", side_effect=mock_filter):
        run_text_filter(txt, output, config, summary_json=summary_json)

    return captured


class TestSummaryContext:
    def test_constant_keywords_injected_without_summary_json(self, tmp_path):
        cfg = _run(tmp_path, _s2_config(constant_keywords=["TestWord"]))
        assert "TestWord" in cfg.get("prompt", "")

    def test_no_context_prompt_unchanged(self, tmp_path):
        cfg = _run(tmp_path, _s2_config())
        assert cfg.get("prompt") == "Base prompt."

    def test_summary_json_summaries_and_keywords_injected(self, tmp_path):
        summary = {
            "summary": "overall",
            "parts": [{"stem": "test", "lines": [1, 1], "summary": "part summary"}],
            "keywords": {"test": ["Dynamic"]},
        }
        cfg = _run(tmp_path, _s2_config(constant_keywords=["Constant"]), summary_data=summary)
        prompt = cfg.get("prompt", "")
        assert "part summary" in prompt
        assert "Constant" in prompt
        assert "Dynamic" in prompt

    def test_other_stem_entries_ignored(self, tmp_path):
        summary = {
            "summary": "overall",
            "parts": [{"stem": "other", "lines": [1, 1], "summary": "not mine"}],
            "keywords": {"other": ["NotMine"]},
        }
        cfg = _run(tmp_path, _s2_config(), summary_data=summary)
        assert cfg.get("prompt") == "Base prompt."

    def test_empty_summary_json_prompt_unchanged(self, tmp_path):
        cfg = _run(
            tmp_path, _s2_config(), summary_data={"summary": "", "parts": [], "keywords": {}}
        )
        assert cfg.get("prompt") == "Base prompt."

    def test_malformed_summary_json_ignored(self, tmp_path):
        cfg = _run(tmp_path, _s2_config(), summary_data="not json {{{")
        assert cfg.get("prompt") == "Base prompt."


def test_disabled_copies_input(tmp_path):
    """When use_llm is false, input is copied to output unchanged."""
    src = tmp_path / "clip.txt"
    src.write_text("l1\nl2\n", encoding="utf-8")
    out = tmp_path / "clip_edits.txt"
    run_text_filter(src, out, get_effective_config(None, {}))
    assert out.read_text(encoding="utf-8") == "l1\nl2\n"
